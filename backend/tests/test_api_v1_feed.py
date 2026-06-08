# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G6.1-T4 SSE feed endpoint.

Covers (issue #310 acceptance criteria):

* ``GET /api/v1/feed`` with a valid JWT returns 200 +
  ``text/event-stream``; ``read_only`` role → 403; unauthenticated → 401.
* Events ``XADD``\\ ed to tenant-A's stream appear on tenant-A's SSE
  connection (per-tenant scoping — stream key derived from JWT).
* Filter by ``op_class`` / ``principal`` / ``target`` keeps only
  matching events.
* ``Last-Event-Id`` header takes precedence over the ``since`` query
  parameter; ``since`` is used when only it is present; ``$`` is the
  default cursor.
* Heartbeat ``: heartbeat\\n\\n`` is emitted after an idle window.
* Malformed entries on the stream don't tear down the subscriber.

Test architecture
=================

Two surfaces, each tested at the layer that fits:

* **HTTP edge (TestFeedEndpoint)** — drives ``/api/v1/feed`` via an
  async httpx client against an ASGI app. Covers authn / authz / the
  200 + content-type happy path. Does not attempt to assert on the
  streamed body — httpx + ASGITransport's cancellation handshake on
  ``async with response.stream(...)`` exit is racy enough that
  asserting on stream contents tight-loops the generator (the mock
  ``xread`` has to ``await asyncio.sleep`` to give cancellation a
  chance, and even then the cancellation propagation through
  Starlette is timing-dependent). Stream contents are exercised at
  the generator layer instead.
* **Generator (TestFeedGenerator)** — drives :func:`_feed_generator`
  directly with a mock broadcast client. Async-generator semantics
  are deterministic: ``aclose()`` synchronously sends
  :class:`asyncio.CancelledError` into the generator's pending
  ``await``, so every test cleanly tears down without relying on
  ASGI's cancellation chain. Every filter / cursor / formatting AC
  is verified here.
* **Real Valkey integration (TestFeedIntegration)** — Docker-gated.
  Publishes via :func:`publish_event` and reads back through the
  generator (NOT via httpx) for the same cancellation-determinism
  reason.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport

from meho_backplane.api.v1.feed import _feed_generator
from meho_backplane.api.v1.feed import router as feed_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    dispose_broadcast_blocking_client,
    dispose_broadcast_client,
    get_broadcast_blocking_client,
    get_broadcast_client,
    publish_event,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.middleware import RequestContextMiddleware
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import (
    AUDIENCE,
    ISSUER,
    make_rsa_keypair,
    mint_token,
    mock_discovery_and_jwks,
    public_jwks,
)

_TENANT_A: UUID = UUID("11111111-1111-1111-1111-111111111111")
_TENANT_B: UUID = UUID("22222222-2222-2222-2222-222222222222")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client() -> Iterator[None]:
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()


@pytest.fixture(autouse=True)
def _empty_backlog_prelude_by_default(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Default the backlog prelude's ``XREVRANGE`` to "no entries".

    G0.16-T3 (#1305) added a backlog prelude that runs whenever the
    generator's cursor is ``$``. Most pre-existing tests in this
    module instantiate ``_feed_generator(..., cursor="$")`` and only
    mock ``xread`` — they never wanted the prelude path to fire and
    have no fixture to handle ``xrevrange``. Patching
    ``redis.asyncio.Redis.xrevrange`` at the class level (rather than
    on a single instance returned by
    :func:`get_broadcast_client`) covers both the prelude-path tests
    that share the client cache and the cases where individual tests
    swap the cached client; defaulting to "stream is empty"
    preserves the legacy assertions byte-for-byte.

    Tests that exercise the prelude branch
    (``TestFeedBacklogPrelude``) override this within their body via
    :func:`patch.object` against the active client; the inner
    instance-level patch wins over the outer class-level patch for
    the duration of the test's ``with`` block, and the autouse
    monkeypatch is restored on teardown either way.

    The real-Valkey ``TestFeedIntegration`` class (Docker-gated)
    explicitly opts out via its own monkeypatch undo — see that
    class's ``valkey_url`` fixture.
    """
    import redis.asyncio as redis

    async def _empty(*_args: object, **_kwargs: object) -> list[object]:
        return []

    monkeypatch.setattr(redis.Redis, "xrevrange", _empty, raising=True)
    yield


@pytest.fixture
def _feed_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin chassis env vars so ``get_settings`` succeeds + JWT verification works.

    ``DATABASE_URL`` is left to the conftest's autouse
    ``_default_database_url`` fixture (per-tmp-path SQLite with
    ``alembic upgrade head`` already run) so the audit middleware's
    INSERT lands on a migrated DB.
    """
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(AuditMiddleware)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(feed_router)
    return app


def _make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://testserver",
    )


def _make_operator(
    *,
    sub: str = "op-test",
    tenant_id: UUID = _TENANT_A,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    """Construct an :class:`Operator` directly — no JWT round-trip.

    The route handler uses ``Depends(require_role(...))`` to produce
    the operator; the generator (where the SSE body lives) takes the
    same shape via plain function arguments. Construction here skips
    the JWT chain and lets generator-level tests pin exactly the
    fields they care about (``sub``, ``tenant_id``).
    """
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=tenant_id,
        tenant_role=role,
    )


def _make_event(
    *,
    tenant_id: UUID = _TENANT_A,
    op_class: str = "read",
    principal_sub: str = "op-test",
    target_name: str | None = "rdc-vcenter",
    op_id: str = "vsphere.vm.list",
) -> BroadcastEvent:
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
    """AsyncMock that returns *events* on the first call, then awaits idle forever.

    Every subsequent call sleeps a tick and returns ``None`` — the
    BLOCK-timeout-shaped response from redis-py ``xread``. The sleep
    is what gives the generator's surrounding code a chance to
    observe :class:`asyncio.CancelledError` when the test calls
    ``gen.aclose()``.
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
    """Read up to *n* SSE frames from *gen* and aclose() it cleanly.

    Tests instantiate ``_feed_generator(...)`` directly, drain the
    first *n* frames, then rely on this helper's
    :meth:`__aiter__.aclose` to cancel any pending ``xread`` and
    unwind the generator. Determinism comes from the mocked ``xread``'s
    ``asyncio.sleep`` yield point above.

    ``TimeoutError`` from the ``asyncio.timeout`` wrapper is caught
    silently because the timeout IS the test's termination mechanism
    when ``n`` is unreachable (e.g. ``n=0`` for cursor-passes-through
    cases, or a heartbeat test waiting one cycle past the patched
    interval). The generator re-raises ``CancelledError`` per its
    post-M1 contract; ``asyncio.timeout`` converts that to
    ``TimeoutError`` on its way out of the context.
    """
    frames: list[str] = []
    if n <= 0:
        # Early-return for cursor-pass-through tests that only need
        # the helper to drive the generator's first ``xread`` call
        # without consuming any frames. The post-``async for``
        # ``aclose`` is still required so the generator's
        # ``CancelledError`` cleanup path runs (and the structured
        # disconnect log fires); without ``aclose`` the generator
        # would be garbage-collected and asyncio 3.13+ would emit
        # ``Task was destroyed but it is pending`` warnings.
        await gen.aclose()
        return frames
    try:
        async with asyncio.timeout(timeout):
            async for chunk in gen:
                frames.append(chunk)
                if len(frames) >= n:
                    break
    except TimeoutError:
        pass
    finally:
        await gen.aclose()
    return frames


async def _drive_generator_with_one_batch(
    items: list[tuple[str, dict[str, str]]],
    *,
    op_class: str | None = None,
    principal: str | None = None,
    target: str | None = None,
) -> list[str]:
    """Mock xread to return *items* once then idle, drive one frame out, return it.

    Skip-path tests (malformed event, unknown field shape) all share
    the same scaffold: push one batch through ``_feed_generator`` and
    assert which entry survives the filter. Bundled here so the
    per-test bodies stop repeating the AsyncMock / patch.object /
    _feed_generator scaffold (SonarCloud duplication-on-new-code).
    """
    broadcast_client = get_broadcast_blocking_client()
    call_count = {"n": 0}

    async def _xread_side_effect(*_a: object, **_k: object) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return [("meho:feed:<irrelevant>", items)]
        await asyncio.sleep(0.01)
        return None

    mock = AsyncMock(side_effect=_xread_side_effect)
    with patch.object(broadcast_client, "xread", new=mock):
        gen = _feed_generator(
            operator=_make_operator(),
            cursor="$",
            op_class=op_class,
            principal=principal,
            target=target,
        )
        return await _collect_n_frames(gen, n=1)


# ---------------------------------------------------------------------------
# HTTP edge — authn / authz / 200 + content-type
# ---------------------------------------------------------------------------


async def _request_feed_authenticated(
    monkeypatch: pytest.MonkeyPatch,
    *,
    kid: str,
    sub: str,
    role: TenantRole,
    extra_headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    """One-call HTTP-edge scaffold: vault + key + JWT + respx + client.

    Collapses the install_fake_vault / make_rsa_keypair / mint_token /
    respx.mock / _make_client chain that every authenticated-edge test
    needs into a single helper. Extracted so the three /api/v1/feed
    edge cases share one body shape instead of three lookalike
    scaffolds (SonarCloud duplication-on-new-code threshold).
    """
    from tests._vault_fakes import install_fake_vault

    install_fake_vault(monkeypatch)
    key = make_rsa_keypair(kid)
    token = mint_token(
        key,
        sub=sub,
        tenant_id=str(_TENANT_A),
        tenant_role=role.value,
    )
    headers = {"Authorization": f"Bearer {token}", **(extra_headers or {})}
    app = _build_app()
    with respx.mock as mock_router:
        mock_discovery_and_jwks(mock_router, public_jwks(key))
        async with _make_client(app) as client:
            return await client.get("/api/v1/feed", headers=headers, params=params)


class TestFeedEndpoint:
    """Authn/authz layer + the 200 + ``text/event-stream`` happy header."""

    async def test_unauthenticated_returns_401(self, _feed_env: None) -> None:
        app = _build_app()
        async with _make_client(app) as client:
            response = await client.get("/api/v1/feed")
        assert response.status_code == 401

    async def test_read_only_role_returns_403(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``read_only`` operators can't subscribe; ``operator`` minimum required."""
        response = await _request_feed_authenticated(
            monkeypatch,
            kid="kid-feed-read-only",
            sub="op-readonly",
            role=TenantRole.READ_ONLY,
        )
        assert response.status_code == 403
        assert response.json() == {"detail": "insufficient_role"}

    async def test_invalid_last_event_id_returns_400(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed ``Last-Event-Id`` cursor → 400, no SSE reconnect loop.

        The bug class this test pins (iter-3 B1): without route-boundary
        validation, garbage cursors propagate to ``XREAD`` and Valkey
        raises ``redis.ResponseError`` mid-stream. ``http.response.start``
        was already sent, so the failure surfaces as a connection drop.
        Per the WHATWG SSE spec, browser ``EventSource`` auto-reconnects
        on stream drop with the SAME ``Last-Event-Id`` — and the bad
        cursor came FROM the client side. Tight reconnect loop with no
        recovery.

        Post-fix contract: an HTTP 400 at the route boundary flips
        ``EventSource.readyState=CLOSED`` per the spec — browsers stop
        auto-reconnecting on 4xx-class responses.
        """
        response = await _request_feed_authenticated(
            monkeypatch,
            kid="kid-feed-bad-cursor",
            sub="op-bad-cursor",
            role=TenantRole.OPERATOR,
            extra_headers={"Last-Event-Id": "abc-not-a-valkey-id"},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("invalid_cursor")

    async def test_invalid_since_query_param_returns_400(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Malformed ``since`` query parameter → 400 (same gate as the header)."""
        response = await _request_feed_authenticated(
            monkeypatch,
            kid="kid-feed-bad-since",
            sub="op-bad-since",
            role=TenantRole.OPERATOR,
            params={"since": "drop-table;"},
        )
        assert response.status_code == 400
        assert response.json()["detail"].startswith("invalid_cursor")

    def test_iso_since_normalises_to_bare_ms_cursor(self) -> None:
        """ISO-8601 ``since`` round-trips to a numeric Valkey-cursor (Finding G).

        G0.16-T6 Finding G (#1312). Per
        ``docs/codebase/api-shape-conventions.md`` §8 the SSE feed
        now accepts the same ISO-8601 shape the MCP
        ``broadcast.recent`` tool already advertised; the helper
        normalises a UTC timestamp to a bare-ms cursor consistent
        with Valkey's stream-id epoch.
        """
        from meho_backplane.api.v1.feed import _validate_cursor_or_400

        # 2026-05-25T10:00:00Z → 1779703200000 ms since epoch
        # (the live timestamp at the instant the operator types it;
        # round-trippable through ``int(dt.timestamp() * 1000)``).
        expected_ms = "1779703200000"
        assert _validate_cursor_or_400("2026-05-25T10:00:00Z") == expected_ms
        # Naive timestamp treated as UTC, not the worker's local TZ.
        assert _validate_cursor_or_400("2026-05-25T10:00:00") == expected_ms
        # TZ-offset preserved through the .timestamp() call.
        assert _validate_cursor_or_400("2026-05-25T12:00:00+02:00") == expected_ms
        # Existing forms still accepted unchanged.
        assert _validate_cursor_or_400("$") == "$"
        assert _validate_cursor_or_400(f"{expected_ms}-0") == f"{expected_ms}-0"
        # A bare date (no ``T``) stays rejected — too easy to typo.
        with pytest.raises(HTTPException) as exc_info:
            _validate_cursor_or_400("2026-05-25")
        assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# ASGI-transport e2e — SSE streams incrementally through the full
# middleware stack (regression coverage for #1389)
# ---------------------------------------------------------------------------


def _sse_frame(event: BroadcastEvent, entry_id: str) -> str:
    """Render *event* as the on-wire ``event: broadcast`` SSE frame."""
    return f"event: broadcast\ndata: {event.model_dump_json()}\nid: {entry_id}\n\n"


class TestSseStreamsThroughMiddleware:
    """The audit middleware forwards ``text/event-stream`` chunks live (#1389).

    Before #1389 :class:`~meho_backplane.audit.AuditMiddleware` buffered
    the entire ASGI response before forwarding it. For an open-ended SSE
    generator the inner-app call never returns, so the buffer is never
    flushed and the client receives **zero bytes** for the life of the
    connection — ``/api/v1/feed`` and ``/ui/broadcast/stream`` are dark
    on a real deploy even though the generator is yielding frames.

    ``httpx.ASGITransport`` cannot prove incremental delivery: it runs
    the inner app **to completion** before exposing the body
    (``handle_async_request`` collects every ``http.response.body`` into
    a list, then wraps it in a stream), so an open-ended generator would
    just hang the transport regardless of whether the middleware buffers
    or streams. The first test therefore drives the production middleware
    stack (``AuditMiddleware`` inside ``RequestContextMiddleware``, the
    ordering :func:`meho_backplane.main` wires) as a **raw ASGI app**
    with an instrumented ``send`` — the only seam that observes a body
    chunk reaching the transport *while the inner generator is still
    suspended*. With the old buffering middleware the handshake below
    deadlocks (no body chunk is forwarded until the generator returns,
    which it never does) → ``asyncio.timeout`` fires → the test fails.

    The second test asserts the audit row is still written for the
    streaming request (fail-closed preserved). It uses a finite generator
    so the assertion rides the normal stream-completion path rather than
    ASGI's racy disconnect-cancellation handshake; ``ASGITransport`` is
    fine here because a finite stream completes on its own.
    """

    async def test_first_chunk_forwarded_before_inner_generator_completes(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A body chunk reaches ``send`` while the SSE generator is still open.

        Drives ``RequestContextMiddleware(AuditMiddleware(app))`` directly
        as an ASGI callable. The patched generator yields one
        ``event: broadcast`` frame and then ``await``\\ s an event that is
        only set *after* the instrumented ``send`` observes the first
        ``http.response.body`` — i.e. the test asserts the body left the
        middleware before the generator could move past its first yield.

        Against the pre-#1389 buffering middleware no body chunk is
        forwarded until the inner app returns, so ``first_body_seen`` is
        never set, the generator blocks on ``release.wait()`` forever, and
        the ``asyncio.timeout`` guard fires → the test fails. The
        streaming fix forwards the chunk live, the handshake completes,
        and the generator is released to finish cleanly.
        """
        from tests._vault_fakes import install_fake_vault

        first_frame = _sse_frame(_make_event(), "1715600000000-0")
        first_body_seen = asyncio.Event()
        release = asyncio.Event()

        async def _one_then_wait(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield first_frame
            # Suspend until the test confirms the first chunk was
            # forwarded — stands in for the never-returning XREAD BLOCK
            # loop. A buffering middleware never sets ``first_body_seen``,
            # so this wait (and thus the whole request) hangs.
            await release.wait()

        import meho_backplane.api.v1.feed as feed_module

        monkeypatch.setattr(feed_module, "_feed_generator", _one_then_wait)

        install_fake_vault(monkeypatch)
        key = make_rsa_keypair("kid-sse-live")
        token = mint_token(
            key,
            sub="op-sse-live",
            tenant_id=str(_TENANT_A),
            tenant_role=TenantRole.OPERATOR.value,
        )

        # Build the production middleware stack around the feed router.
        inner = FastAPI()
        inner.include_router(feed_router)
        asgi_app = RequestContextMiddleware(AuditMiddleware(inner))

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/v1/feed",
            "raw_path": b"/api/v1/feed",
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("testclient", 50000),
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        request_drained = False

        async def receive() -> dict[str, object]:
            # Deliver the (empty) request body exactly once, then behave
            # like a still-connected client: block until the test releases
            # the request, then report ``http.disconnect``. A real ASGI
            # server's ``receive`` blocks here too — Starlette's
            # ``StreamingResponse`` spawns a disconnect-listener task that
            # polls ``receive``; returning ``http.request`` on every call
            # would busy-loop that task at 100% CPU.
            nonlocal request_drained
            if not request_drained:
                request_drained = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await release.wait()
            return {"type": "http.disconnect"}

        captured_status: dict[str, int] = {}
        body_chunks: list[bytes] = []

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                captured_status["status"] = int(message["status"])  # type: ignore[arg-type]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                if body:
                    body_chunks.append(body)  # type: ignore[arg-type]
                    first_body_seen.set()

        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            task = asyncio.ensure_future(asgi_app(scope, receive, send))
            try:
                # Streaming fix: the first body chunk is forwarded live,
                # so this resolves immediately. Buffering: never resolves.
                async with asyncio.timeout(5.0):
                    await first_body_seen.wait()
                assert captured_status["status"] == 200
                assert body_chunks, "no body chunk forwarded before generator suspended"
                assert b"event: broadcast" in b"".join(body_chunks)
            finally:
                # Release the suspended generator so the request task can
                # complete and we don't leak a pending task.
                release.set()
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(5.0):
                        await task

    async def test_streaming_request_writes_audit_row(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An audit row is written for a streaming request (fail-closed preserved).

        Uses a *finite* generator (two frames, then the stream ends) so
        the middleware's audit-write runs on normal stream completion
        rather than depending on ASGI's racy disconnect-cancellation
        handshake. After the body is fully drained the ``audit_log``
        table carries exactly one row for ``GET /api/v1/feed`` with
        ``status_code=200`` — proving the SSE path keeps the
        every-authenticated-action-gets-a-row contract.
        """
        from sqlalchemy import select

        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import AuditLog

        frames = [
            _sse_frame(_make_event(op_id="vsphere.vm.list"), "1715600000000-0"),
            _sse_frame(_make_event(op_id="vsphere.vm.get"), "1715600000001-0"),
        ]

        async def _finite_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            for frame in frames:
                yield frame

        import meho_backplane.api.v1.feed as feed_module

        monkeypatch.setattr(feed_module, "_feed_generator", _finite_stream)

        app = _build_app()
        with respx.mock as mock_router:
            from tests._vault_fakes import install_fake_vault

            install_fake_vault(monkeypatch)
            key = make_rsa_keypair("kid-sse-audit")
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            token = mint_token(
                key,
                sub="op-sse-audit",
                tenant_id=str(_TENANT_A),
                tenant_role=TenantRole.OPERATOR.value,
            )
            async with _make_client(app) as client:
                response = await client.get(
                    "/api/v1/feed",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                # Both frames must be present in the streamed body — the
                # middleware forwarded them, it did not drop the stream.
                assert response.text.count("event: broadcast") == 2

        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = list(
                (await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/feed")))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].operator_sub == "op-sse-audit"
        assert rows[0].method == "GET"
        assert rows[0].status_code == 200

    async def test_double_cancelled_disconnect_still_writes_audit_row(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A second cancellation mid-INSERT does not drop the disconnect-path row (#1599).

        Drives ``RequestContextMiddleware(AuditMiddleware(app))`` as a raw
        ASGI callable (the same seam as
        ``test_first_chunk_forwarded_before_inner_generator_completes``)
        and reproduces a production SSE disconnect by **cancelling the ASGI
        task** while the feed generator is suspended — which is how a real
        ASGI server (uvicorn, ``asgi.spec_version >= 2.4``) signals a
        client disconnect: it cancels the task running the app. That
        cancellation raises out of the suspended generator, past
        ``_run_inner_app_buffered``'s ``except Exception`` (``CancelledError``
        is a ``BaseException``), and into ``AuditMiddleware.__call__``'s
        ``except asyncio.CancelledError`` arm — the disconnect-path audit
        write under test.

        The ``http.disconnect`` / ``receive``-listener route is *not* used:
        under Starlette's ``spec_version < 2.4`` task-group branch the
        disconnect listener cancels the stream inside the response's own
        anyio cancel scope, which absorbs the ``CancelledError`` so the
        response returns normally and the row is written on the *normal*
        path — never exercising the ``except`` arm. Pinning
        ``spec_version="2.4"`` selects the branch that streams directly, so
        a task-level cancel propagates the way a real disconnect does.

        While the disconnect-path INSERT is in flight, the instrumented
        write delivers a **second** ``task.cancel()`` — the task-tree
        teardown / server-shutdown / enclosing-``timeout()`` interleaving
        the issue describes. With the shielded write the row still commits:
        the cancellation hits ``await asyncio.shield(write)`` (not the write
        task), the fix drains the write to completion, then re-raises so
        cancellation is honored. The post-run ``audit_log`` table carries
        exactly one row for ``GET /api/v1/feed``.

        This test *discriminates*: against a variant where the
        disconnect-path ``_finalize`` is awaited unshielded, the second
        cancellation interrupts the in-flight ``_write_audit_row`` await
        (``CancelledError`` bypasses ``_finalize``'s ``except Exception``
        arm) and zero rows are written → ``write_committed`` stays False and
        ``len(rows) == 1`` fails. Adversarially verified against that
        broken variant during implementation.
        """
        from sqlalchemy import select

        import meho_backplane.audit as audit_module
        from meho_backplane.db.engine import get_sessionmaker
        from meho_backplane.db.models import AuditLog
        from tests._vault_fakes import install_fake_vault

        first_frame = _sse_frame(_make_event(), "1715600000000-0")
        first_body_seen = asyncio.Event()
        # Set only in cleanup. The generator must NOT return on its own —
        # the *only* way out of its post-yield suspension is the disconnect
        # cancellation that drives ``__call__``'s ``except`` arm. A
        # generator that could return would race the normal stream-end path
        # and the test would no longer isolate the disconnect branch.
        teardown = asyncio.Event()

        async def _one_then_block(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield first_frame
            await teardown.wait()

        import meho_backplane.api.v1.feed as feed_module

        monkeypatch.setattr(feed_module, "_feed_generator", _one_then_block)

        # The ASGI task is bound below; the in-flight write closes over it.
        task_holder: dict[str, asyncio.Task[None]] = {}
        write_committed = False

        # Wrap the audit INSERT so the SECOND cancellation is delivered
        # *while the disconnect-path write is in flight*. By the time this
        # runs we are provably inside ``__call__``'s ``except CancelledError``
        # arm — it is the only caller of ``_finalize`` for a generator that
        # never returns — so cancelling the task here is the "second cancel
        # mid-INSERT" the issue describes, with no race against the normal
        # path.
        real_write_audit_row = audit_module._write_audit_row

        async def _instrumented_write(**kwargs: object) -> None:
            nonlocal write_committed
            # Deliver the second cancellation, then yield so it is raised at
            # the parent's ``await asyncio.shield(write)`` before this
            # shielded write proceeds. ``shield`` keeps the cancel off this
            # (the write) task; the fix drains us to completion here.
            task_holder["task"].cancel()
            await asyncio.sleep(0)
            await real_write_audit_row(**kwargs)  # type: ignore[arg-type]
            write_committed = True

        monkeypatch.setattr(audit_module, "_write_audit_row", _instrumented_write)

        install_fake_vault(monkeypatch)
        key = make_rsa_keypair("kid-sse-dbl-cancel")
        token = mint_token(
            key,
            sub="op-sse-dbl-cancel",
            tenant_id=str(_TENANT_A),
            tenant_role=TenantRole.OPERATOR.value,
        )

        inner = FastAPI()
        inner.include_router(feed_router)
        asgi_app = RequestContextMiddleware(AuditMiddleware(inner))

        scope = {
            "type": "http",
            # ``spec_version="2.4"`` selects Starlette's direct-stream
            # branch (no disconnect-listener cancel scope), so a task-level
            # cancel propagates the way a real server-signalled disconnect
            # does — into the middleware's ``except`` arm.
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/v1/feed",
            "raw_path": b"/api/v1/feed",
            "query_string": b"",
            "root_path": "",
            "scheme": "https",
            "server": ("testserver", 443),
            "client": ("testclient", 50000),
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }

        request_drained = False

        async def receive() -> dict[str, object]:
            # Drain the (empty) request body once, then behave like a
            # still-connected client (block). The disconnect is signalled by
            # cancelling the ASGI task, not via this channel.
            nonlocal request_drained
            if not request_drained:
                request_drained = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await teardown.wait()
            return {"type": "http.disconnect"}

        captured_status: dict[str, int] = {}

        async def send(message: dict[str, object]) -> None:
            if message["type"] == "http.response.start":
                captured_status["status"] = int(message["status"])  # type: ignore[arg-type]
            elif message["type"] == "http.response.body" and message.get("body"):
                first_body_seen.set()

        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            task = asyncio.ensure_future(asgi_app(scope, receive, send))
            task_holder["task"] = task
            try:
                # 1. Let the first SSE frame reach the wire (response has
                #    started; the generator is now suspended on ``teardown``).
                async with asyncio.timeout(5.0):
                    await first_body_seen.wait()
                assert captured_status["status"] == 200

                # 2. Signal the client disconnect by cancelling the task
                #    (FIRST cancel). This raises out of the suspended
                #    generator into the middleware's ``except`` arm, which
                #    schedules the shielded write; the instrumented INSERT
                #    then delivers the SECOND cancel mid-write.
                task.cancel()

                # 3. The task must end cancelled (cancellation honored, not
                #    suppressed).
                with pytest.raises(asyncio.CancelledError):
                    async with asyncio.timeout(5.0):
                        await task
            finally:
                teardown.set()

        # The shielded write committed the row despite the double cancel.
        assert write_committed, "disconnect-path audit write did not run to completion"

        # Exactly one audit row for the disconnected feed request — no
        # silent drop.
        sessionmaker = get_sessionmaker()
        async with sessionmaker() as session:
            rows = list(
                (await session.execute(select(AuditLog).where(AuditLog.path == "/api/v1/feed")))
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].operator_sub == "op-sse-dbl-cancel"
        assert rows[0].method == "GET"


# ---------------------------------------------------------------------------
# Generator — every body-shaping AC
# ---------------------------------------------------------------------------


class TestFeedGenerator:
    """Drives :func:`_feed_generator` directly with a mocked broadcast client."""

    async def test_basic_event_yield_with_tenant_scoping(self, _feed_env: None) -> None:
        """One event → one well-formed SSE frame; stream key is JWT-derived."""
        event = _make_event(tenant_id=_TENANT_A)
        broadcast_client = get_broadcast_blocking_client()
        mock = _xread_returning([event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert mock.await_count >= 1
        # Stream key passed to xread is the JWT-derived tenant stream.
        stream_key_arg = mock.await_args_list[0].args[0]
        assert stream_key_arg == {f"meho:feed:{_TENANT_A}": "$"}

        assert len(frames) == 1
        frame = frames[0]
        assert frame.startswith("event: broadcast\n")
        assert "data: " in frame
        assert "id: 1715600000000-0" in frame
        data_line = next(line for line in frame.split("\n") if line.startswith("data: "))
        decoded = json.loads(data_line[len("data: ") :])
        assert decoded["tenant_id"] == str(_TENANT_A)
        assert decoded["op_id"] == "vsphere.vm.list"

    async def test_filter_by_op_class(self, _feed_env: None) -> None:
        read_event = _make_event(op_class="read", op_id="vsphere.vm.list")
        write_event = _make_event(op_class="write", op_id="vsphere.vm.create")
        broadcast_client = get_broadcast_blocking_client()
        mock = _xread_returning([read_event, write_event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(),
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

    async def test_filter_by_principal(self, _feed_env: None) -> None:
        a_event = _make_event(principal_sub="alice")
        b_event = _make_event(principal_sub="bob")
        broadcast_client = get_broadcast_blocking_client()
        mock = _xread_returning([a_event, b_event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(),
                cursor="$",
                op_class=None,
                principal="alice",
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert len(frames) == 1
        decoded = json.loads(
            next(line for line in frames[0].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert decoded["principal_sub"] == "alice"

    async def test_filter_by_target(self, _feed_env: None) -> None:
        vcenter_event = _make_event(target_name="rdc-vcenter")
        k8s_event = _make_event(target_name="rdc-k8s")
        no_target_event = _make_event(target_name=None)
        broadcast_client = get_broadcast_blocking_client()
        mock = _xread_returning([vcenter_event, k8s_event, no_target_event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(),
                cursor="$",
                op_class=None,
                principal=None,
                target="rdc-vcenter",
            )
            frames = await _collect_n_frames(gen, n=1)

        # Only the vCenter event matches; events with target=None are
        # filtered out when the operator asks for a specific target.
        assert len(frames) == 1
        decoded = json.loads(
            next(line for line in frames[0].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert decoded["target_name"] == "rdc-vcenter"

    async def test_heartbeat_emitted_when_all_entries_filter_out(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Busy-but-filtered tenants still receive the keep-alive heartbeat.

        The bug class this test pins (B1 / SonarCloud iter-1): a noisy
        tenant where every event is rejected by the subscriber's filter
        would otherwise emit zero outbound bytes but reset
        ``last_heartbeat`` on every xread cycle — the wall-clock idle
        check would never fire and intermediaries (nginx 60s, AWS ALB
        60s, CloudFront 60s) would idle-timeout the connection. The
        post-fix contract: heartbeats track outbound silence, not
        inbound stream activity.

        Time is compressed via the ``_HEARTBEAT_INTERVAL_SECONDS``
        constant patched to a sub-second value rather than letting the
        test wait the production 30 s. Same approach the SonarCloud
        S7483 comment about timeout context managers anticipates.
        """
        from meho_backplane.api.v1 import feed as feed_module

        monkeypatch.setattr(feed_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)

        # Every event has op_class=write; the subscriber filters for
        # op_class=read. xread returns the events on cycle 1; cycle 2+
        # returns None with a sleep that's long enough for the patched
        # heartbeat window to elapse.
        write_event_a = _make_event(op_class="write", op_id="vsphere.vm.create")
        write_event_b = _make_event(op_class="write", op_id="vsphere.vm.delete")
        broadcast_client = get_broadcast_blocking_client()
        items = [
            (f"{1715600000000 + i}-0", {"event": event.model_dump_json()})
            for i, event in enumerate([write_event_a, write_event_b])
        ]
        call_count = {"n": 0}

        async def _xread_side_effect(*_a: object, **_k: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                # All entries filter out: subscriber asked for reads,
                # tenant sent writes.
                return [("meho:feed:<irrelevant>", items)]
            # Subsequent cycles sleep past the heartbeat interval so
            # the next loop iteration emits a heartbeat.
            await asyncio.sleep(0.1)
            return None

        mock = AsyncMock(side_effect=_xread_side_effect)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(),
                cursor="$",
                op_class="read",
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1, timeout=2.0)

        # The single emitted frame is the heartbeat — every event was
        # filtered out, but the contract still required keep-alive
        # bytes on the wire. ``_collect_n_frames`` appends the raw
        # yielded chunk verbatim (no \n\n splitting), so the frame
        # carries the SSE comment-line shape end-to-end.
        assert len(frames) == 1
        assert frames[0] == ": heartbeat\n\n"

    async def test_cursor_advances_past_filtered_entries(
        self,
        _feed_env: None,
    ) -> None:
        """Filtered entries advance the XREAD cursor so the next BLOCK reads past them.

        The bug class this test pins: ``_process_entries`` consumes
        filtered-out / malformed / unknown-field-shape entries
        internally without yielding. A naive ``cursor = entry_id``
        placed inside the main loop's post-helper ``for ... yield``
        would never observe those entries, leaving the cursor pinned
        at the previous value. Under explicit-cursor replay
        (``since=<id>`` or ``Last-Event-Id``) on a busy-but-filtered
        tenant, the next XREAD would re-read the same batch
        indefinitely.

        Post-fix contract: the cursor advances to ``items[-1][0]`` for
        every consumed batch BEFORE the yield loop. The second XREAD
        call must therefore start *past* the first batch's last entry,
        not at the original subscriber cursor.
        """
        # First batch: two writes (subscriber filters for reads).
        # Second batch: one read.
        write_a = _make_event(op_class="write", op_id="vsphere.vm.create")
        write_b = _make_event(op_class="write", op_id="vsphere.vm.delete")
        read_c = _make_event(op_class="read", op_id="vsphere.vm.list")
        broadcast_client = get_broadcast_blocking_client()
        seen_cursors: list[str] = []

        async def _xread_side_effect(
            streams: dict[str, str],
            **_kwargs: object,
        ) -> object:
            cursor_value = next(iter(streams.values()))
            seen_cursors.append(cursor_value)
            if len(seen_cursors) == 1:
                return [
                    (
                        "meho:feed:<irrelevant>",
                        [
                            ("1715600000001-0", {"event": write_a.model_dump_json()}),
                            ("1715600000002-0", {"event": write_b.model_dump_json()}),
                        ],
                    ),
                ]
            if len(seen_cursors) == 2:
                return [
                    (
                        "meho:feed:<irrelevant>",
                        [("1715600000003-0", {"event": read_c.model_dump_json()})],
                    ),
                ]
            await asyncio.sleep(0.01)
            return None

        mock = AsyncMock(side_effect=_xread_side_effect)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(),
                cursor="1715600000000-0",
                op_class="read",
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        # The yielded frame is the read event from the SECOND batch —
        # the writes in batch one filtered out and produced no
        # outbound frames.
        assert len(frames) == 1
        assert "id: 1715600000003-0" in frames[0]

        # Load-bearing assertion: the second XREAD call's cursor is
        # the LAST entry id of batch one, NOT the original
        # subscriber cursor. Without the iter-2 B1 fix, the second
        # call would resend ``1715600000000-0`` and Valkey would
        # re-return the filtered writes forever.
        assert len(seen_cursors) >= 2
        assert seen_cursors[0] == "1715600000000-0"
        assert seen_cursors[1] == "1715600000002-0"

    async def test_cursor_passes_through_to_xread(self, _feed_env: None) -> None:
        """The cursor argument (already resolved by the handler) is what xread sees."""
        broadcast_client = get_broadcast_blocking_client()
        event = _make_event(tenant_id=_TENANT_A)
        mock = _xread_returning([event])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="1715600000000-0",
                op_class=None,
                principal=None,
                target=None,
            )
            await _collect_n_frames(gen, n=1, timeout=0.2)

        assert mock.await_count >= 1
        cursor_arg = mock.await_args_list[0].args[0]
        assert cursor_arg == {f"meho:feed:{_TENANT_A}": "1715600000000-0"}

    async def test_malformed_event_skipped(self, _feed_env: None) -> None:
        """A bogus JSON in ``event`` doesn't kill the subscriber."""
        good_event = _make_event(op_id="vsphere.vm.list")
        items = [
            ("1715600000000-0", {"event": "not-json"}),
            ("1715600000001-0", {"event": good_event.model_dump_json()}),
        ]
        frames = await _drive_generator_with_one_batch(items)
        assert len(frames) == 1
        assert "id: 1715600000001-0" in frames[0]

    async def test_unknown_field_shape_skipped(self, _feed_env: None) -> None:
        """An XADD'd entry without an ``event`` field is logged + skipped."""
        good_event = _make_event(op_id="vsphere.host.info")
        items = [
            ("1715600000000-0", {"unknown": "field"}),
            ("1715600000001-0", {"event": good_event.model_dump_json()}),
        ]
        frames = await _drive_generator_with_one_batch(items)
        assert len(frames) == 1
        assert "id: 1715600000001-0" in frames[0]

    async def test_xread_connection_error_emits_feed_error_then_closes(
        self,
        _feed_env: None,
    ) -> None:
        """Transport-down on first XREAD → T11-compliant SSE error frame, clean close.

        The bug class this test pins (G0.14-T5 #1146 / signal 10 of
        ``claude-rdc-hetzner-dc#697``): an unguarded ``await
        client.xread(...)`` propagated a ``redis.exceptions.RedisError``
        family member through Starlette into FastAPI's default handler.
        Because the SSE response had already sent
        ``http.response.start``, FastAPI could not swap to a 5xx body
        — the consumer saw a bare HTTP 500 with no JSON envelope.

        Post-fix contract: the failure is caught inside
        ``_feed_generator``; one structured ``event: feed_error`` frame
        is yielded carrying the T11 three-clause shape (code +
        component-naming message + doc reference); the loop ``break``\\ s
        so the generator's ``__anext__`` raises ``StopAsyncIteration``
        next and Starlette closes the SSE stream cleanly.
        """
        import redis.exceptions as redis_exc

        broadcast_client = get_broadcast_blocking_client()
        mock = AsyncMock(side_effect=redis_exc.ConnectionError("connection refused"))
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames: list[str] = []
            async for chunk in gen:
                frames.append(chunk)
            # Generator exhausts on its own after the error frame —
            # no aclose() needed; the ``break`` path falls through
            # the outer ``try`` to natural termination.

        assert len(frames) == 1
        frame = frames[0]
        assert frame.startswith("event: feed_error\n")
        assert "\nid:" not in frame  # error frames are not part of the replay cursor
        data_line = next(line for line in frame.split("\n") if line.startswith("data: "))
        decoded = json.loads(data_line[len("data: ") :])
        # T11 three-clause: stable snake_case code, component-naming
        # human message with a doc reference, doc path the operator
        # can resolve from their checked-out clone.
        assert decoded["code"] == "broadcast_subsystem_unavailable"
        assert decoded["doc"] == "docs/codebase/error-message-shape.md"
        assert f"meho:feed:{_TENANT_A}" in decoded["message"]
        assert "ConnectionError" in decoded["message"]
        assert "docs/codebase/error-message-shape.md" in decoded["message"]

    async def test_xread_response_error_emits_feed_error_then_closes(
        self,
        _feed_env: None,
    ) -> None:
        """``ResponseError`` (e.g. wrong-type-at-key) also produces the structured frame.

        ``redis.exceptions.ResponseError`` is the redis-py shape Valkey
        returns when an XREAD lands against a key that exists but
        carries a non-stream value (a schema-level misconfiguration
        the operator's ``BROADCAST_REDIS_URL`` could land in). Same
        operator-side remediation as ``ConnectionError`` ("check the
        broadcast subsystem and read the doc"); same SSE error frame.
        """
        import redis.exceptions as redis_exc

        broadcast_client = get_broadcast_blocking_client()
        mock = AsyncMock(
            side_effect=redis_exc.ResponseError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )
        )
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames: list[str] = []
            async for chunk in gen:
                frames.append(chunk)

        assert len(frames) == 1
        data_line = next(line for line in frames[0].split("\n") if line.startswith("data: "))
        decoded = json.loads(data_line[len("data: ") :])
        assert decoded["code"] == "broadcast_subsystem_unavailable"
        assert "ResponseError" in decoded["message"]
        # The raw redis-py message is **not** echoed — info-leak
        # boundary from the T11 doc keeps transport-level prose
        # (which can name broker hostnames, internal IPs) out of the
        # response body. Only the exception class name lands.
        assert "WRONGTYPE" not in decoded["message"]

    async def test_transport_timeout_mid_stream_emits_feed_error_after_prior_events(
        self,
        _feed_env: None,
    ) -> None:
        """First call returns events, second raises ``TimeoutError`` → events then error frame.

        Post RDC #789 N1 / Initiative #1353: ``redis.TimeoutError`` from
        ``xread`` no longer fires at ~5 s on a quiet stream — the
        blocking client's 35 s ``socket_timeout`` exceeds the 30 s
        ``XREAD BLOCK`` window, so a quiet BLOCK expires naturally and
        ``xread`` returns ``None`` (covered by the new
        :meth:`test_quiet_stream_block_timeout_yields_no_error_frame`).
        A ``TimeoutError`` propagating out of ``xread`` therefore now
        signals a *genuine* transport failure: socket dead longer than
        the configured ``socket_timeout``. That remains a
        ``broadcast_subsystem_unavailable`` condition — the operator
        side cannot distinguish "blocked too long" from "broker died"
        and the remediation is the same (chase the broadcast pod /
        network); the SSE consumer sees a single ``feed_error`` frame
        and a clean close.
        """
        import redis.exceptions as redis_exc

        broadcast_client = get_broadcast_blocking_client()
        event = _make_event(tenant_id=_TENANT_A)
        items = [("1715600000000-0", {"event": event.model_dump_json()})]
        call_count = {"n": 0}

        async def _xread_side_effect(*_a: object, **_k: object) -> object:
            call_count["n"] += 1
            if call_count["n"] == 1:
                return [(f"meho:feed:{_TENANT_A}", items)]
            # Genuine transport timeout — socket dead past the blocking
            # client's 35 s socket_timeout. Distinct from "BLOCK
            # expired naturally on a quiet stream" (xread returns None),
            # which the post-fix generator handles via the heartbeat
            # path without an error frame.
            raise redis_exc.TimeoutError("transport socket timed out")

        mock = AsyncMock(side_effect=_xread_side_effect)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames: list[str] = []
            async for chunk in gen:
                frames.append(chunk)

        # First frame is the event from the successful XREAD; second
        # is the error frame from the failing one. Order matters —
        # the consumer sees real events up until the failure point.
        assert len(frames) == 2
        assert frames[0].startswith("event: broadcast\n")
        assert frames[1].startswith("event: feed_error\n")
        decoded = json.loads(
            next(line for line in frames[1].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert decoded["code"] == "broadcast_subsystem_unavailable"
        assert "TimeoutError" in decoded["message"]

    async def test_quiet_stream_block_timeout_yields_no_error_frame(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``xread`` returning ``None`` (quiet-stream BLOCK expiry) → heartbeat, NOT ``feed_error``.

        Pre RDC #789 N1: redis-py's 5 s ``socket_timeout`` (pinned on
        the single process-wide client) raised
        ``redis.TimeoutError`` from inside the 30 s ``XREAD BLOCK`` on
        every fresh SSE connection within ~5 s, producing a spurious
        ``feed_error`` frame on a healthy substrate.

        Post-fix: the blocking client's 35 s ``socket_timeout`` exceeds
        the 30 s BLOCK window, so a quiet stream's BLOCK now expires
        naturally and ``xread`` returns ``None`` — the
        :func:`_consume_xread_batch` "no entries" path. The generator
        treats that as the keep-alive signal: the next loop iteration
        emits a heartbeat once
        :data:`_HEARTBEAT_INTERVAL_SECONDS` of outbound silence has
        elapsed, never a ``feed_error``. Asserted here at the
        generator layer with a sub-second heartbeat interval so the
        test exits in milliseconds instead of the production 30 s.
        """
        from meho_backplane.api.v1 import feed as feed_module

        # Sub-second heartbeat interval so the test exits quickly.
        monkeypatch.setattr(feed_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)

        broadcast_client = get_broadcast_blocking_client()
        call_count = {"n": 0}

        async def _xread_side_effect(*_a: object, **_k: object) -> object:
            call_count["n"] += 1
            # Mirror redis-py's BLOCK-expired-naturally contract: an
            # await point yields to the loop, then None comes back
            # (no entries arrived inside the BLOCK window).
            await asyncio.sleep(0.1)
            return None

        mock = AsyncMock(side_effect=_xread_side_effect)
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1, timeout=2.0)

        # Exactly the heartbeat — never a ``feed_error`` frame on a
        # quiet stream after the fix. This is the test that would have
        # *failed* against the pre-fix shape (where xread raised
        # TimeoutError at ~5 s and the generator yielded the
        # broadcast_subsystem_unavailable frame).
        assert len(frames) == 1
        assert frames[0] == ": heartbeat\n\n"
        # The mock was called at least once (the BLOCK loop entered);
        # asserting on call count > 0 keeps the test robust to the
        # event loop scheduling between the heartbeat emission and
        # the test's collect-loop exit.
        assert mock.await_count >= 1

    async def test_fresh_dollar_quiet_stream_survives_past_fast_socket_timeout(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A fresh ``$`` SSE reader survives a >5 s quiet window without ``feed_error``.

        Direct repro of the RDC #789 N1 consumer signal: open a
        ``GET /api/v1/feed`` (cursor=``$``) against a tenant with no
        new writes; pre-fix, the connection died at ~5 s with a
        spurious ``broadcast_subsystem_unavailable`` frame because
        ``socket_timeout=5.0`` (on the single process-wide client)
        was shorter than the 30 s ``XREAD BLOCK`` window.

        Post-fix, the blocking client's ``socket_timeout=35 s`` lets
        the BLOCK expire naturally (``xread`` returns ``None``) and
        the generator emits a heartbeat. This test compresses time
        (sub-second BLOCK and heartbeat intervals) and asserts on the
        relevant shape: a quiet window longer than the *fast*
        client's 5 s timeout produces a heartbeat, not a
        ``feed_error``.

        Pinned to the prelude path's empty-stream branch so the test
        covers the fresh-``$`` connection shape end to end: prelude
        ``XREVRANGE`` returns no entries (empty stream) → BLOCK loop
        enters with cursor ``$`` → ``xread`` returns ``None`` after
        the BLOCK window → heartbeat.
        """
        from meho_backplane.api.v1 import feed as feed_module

        # Sub-second cadence so the test runs in milliseconds, not 35 s.
        monkeypatch.setattr(feed_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.05)

        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        # Empty XREVRANGE — fresh tenant with no backlog entries.
        prelude = AsyncMock(return_value=[])

        async def _quiet_block(*_a: object, **_k: object) -> object:
            # Quiet BLOCK: wait longer than the fast client's 5 s
            # socket_timeout pre-fix would have allowed, then return
            # None (BLOCK expired naturally). The compressed sleep
            # here stands in for the real 30 s BLOCK window; the
            # contract under test is "None from xread → heartbeat,
            # never a feed_error".
            await asyncio.sleep(0.1)
            return None

        idle_xread = AsyncMock(side_effect=_quiet_block)
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1, timeout=2.0)

        # The single observable frame is the keep-alive heartbeat —
        # never a ``broadcast_subsystem_unavailable`` ``feed_error``.
        assert len(frames) == 1
        assert frames[0] == ": heartbeat\n\n"
        for frame in frames:
            assert "broadcast_subsystem_unavailable" not in frame
            assert "feed_error" not in frame


# ---------------------------------------------------------------------------
# Backlog prelude (G0.16-T3 #1305 — SSE feed delivers zero bytes fix)
# ---------------------------------------------------------------------------


def _idle_xread_mock() -> AsyncMock:
    """An ``xread`` mock that yields to the event loop, returns ``None`` forever.

    Used by :class:`TestFeedBacklogPrelude` to assert prelude /
    BLOCK-loop interactions without the generator spinning
    uncancellably. ``AsyncMock(return_value=None)`` returns
    synchronously, so a generator's ``while True: await xread(...)``
    would never suspend and the test's ``asyncio.timeout``
    surrounding ``_collect_n_frames`` could never preempt the loop
    (the runner core stays pinned until pytest declares the worker
    lost). The ``asyncio.sleep`` yield point is what makes the
    cancellation contract observable inside the same task tree.
    Mirrors the side-effect dance inside :func:`_xread_returning`.
    """

    async def _side_effect(*_a: object, **_k: object) -> None:
        await asyncio.sleep(0.01)
        return None

    return AsyncMock(side_effect=_side_effect)


class TestFeedBacklogPrelude:
    """``_emit_backlog_prelude`` + the ``$``-cursor entry path on ``_feed_generator``.

    These tests pin the v0.8.0 → v0.9.0 fix for
    ``claude-rdc-hetzner-dc#771`` Finding 14: a fresh
    ``GET /api/v1/feed`` against a tenant with existing entries on
    the stream must surface those entries within bounded latency.
    Pre-fix, the generator's ``$`` cursor unconditionally skipped
    backlog AND the 30 s heartbeat cadence meant ``curl`` saw 0
    bytes inside its default 8 s timeout.
    """

    async def test_fresh_dollar_connection_replays_backlog_chronologically(
        self,
        _feed_env: None,
    ) -> None:
        """``cursor="$"`` → prelude yields backlog before BLOCK starts.

        Mirrors the consumer repro: the stream has 3 entries, the
        operator opens an SSE connection, the first three SSE frames
        carry those events in chronological order. The XREVRANGE
        result comes in reverse order from Valkey; the prelude
        helper is responsible for the reversal, so the assertion is
        on the frame order, not the XREVRANGE input order.
        """
        events = [
            _make_event(op_id="audit.first"),
            _make_event(op_id="audit.second"),
            _make_event(op_id="audit.third"),
        ]
        # XREVRANGE returns latest-first by entry id: the largest id
        # comes first. ``audit.third`` was published last so it
        # carries the largest id (``...002``); ``audit.first`` the
        # smallest (``...000``). The prelude helper reverses this
        # back into chronological order for the SSE consumer.
        xrevrange_items = [
            ("1715600000002-0", {"event": events[2].model_dump_json()}),
            ("1715600000001-0", {"event": events[1].model_dump_json()}),
            ("1715600000000-0", {"event": events[0].model_dump_json()}),
        ]
        # Two-client split per RDC #789 N1 / Initiative #1353: prelude
        # XREVRANGE uses the fast (5 s socket_timeout) client; BLOCK
        # XREAD uses the long-poll (35 s socket_timeout) client. Tests
        # mirror that split.
        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        # Idle XREAD with a yield point — see :func:`_idle_xread_mock`'s
        # docstring for the cancellation rationale (a bare
        # ``AsyncMock(return_value=None)`` pins the runner core).
        idle_xread = _idle_xread_mock()
        prelude = AsyncMock(return_value=xrevrange_items)
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=3, timeout=2.0)

        assert prelude.await_count == 1
        # XREVRANGE called with the tenant stream key + the prelude
        # cap. redis-py 7.x exposes ``count`` as a keyword-only arg.
        call = prelude.await_args_list[0]
        assert call.args[0] == f"meho:feed:{_TENANT_A}"
        assert call.kwargs.get("count") == 50
        assert len(frames) == 3
        op_ids = [
            json.loads(
                next(line for line in frame.split("\n") if line.startswith("data: "))[
                    len("data: ") :
                ]
            )["op_id"]
            for frame in frames
        ]
        assert op_ids == ["audit.first", "audit.second", "audit.third"]
        for frame in frames:
            # The prelude reuses ``_format_event`` so frames are
            # indistinguishable from live-tail frames at the wire
            # level — same prefix, same id line shape.
            assert frame.startswith("event: broadcast\n")
            assert "\nid: " in frame

    async def test_prelude_advances_block_cursor_past_replayed_entries(
        self,
        _feed_env: None,
    ) -> None:
        """After prelude, BLOCK loop reads from the last replayed entry id, not ``$``.

        Without this advance, the BLOCK loop's first XREAD would
        observe an XADD made between the prelude and the BLOCK
        landing as "new", but the prelude already shipped earlier
        entries past which the cursor should now sit. The assertion
        is that the BLOCK loop's first XREAD argument carries the
        last-replayed entry id as the cursor, not ``$``.
        """
        event = _make_event()
        xrevrange_items = [
            ("1715600000005-0", {"event": event.model_dump_json()}),
        ]
        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        idle_xread = _idle_xread_mock()
        prelude = AsyncMock(return_value=xrevrange_items)
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            # Consume the prelude frame AND let the generator land
            # in the BLOCK loop's first XREAD await before the
            # timeout fires. Asking for n=2 reaches the BLOCK loop
            # because the prelude yields exactly 1 frame; the
            # ``async for`` then suspends on ``client.xread`` until
            # the timeout cancels the iteration.
            await _collect_n_frames(gen, n=2, timeout=0.3)

        assert idle_xread.await_count >= 1
        first_xread_streams = idle_xread.await_args_list[0].args[0]
        assert first_xread_streams == {f"meho:feed:{_TENANT_A}": "1715600000005-0"}

    async def test_empty_stream_prelude_is_noop_and_block_runs_from_dollar(
        self,
        _feed_env: None,
    ) -> None:
        """No history on stream → prelude yields zero frames, BLOCK reads from ``$``.

        A fresh tenant whose stream is empty should fall through to
        the live-tail behaviour pre-#1305 — no prelude frames, BLOCK
        loop's first XREAD uses ``$``.
        """
        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        # Empty xrevrange = empty stream.
        prelude = AsyncMock(return_value=[])
        idle_xread = _idle_xread_mock()
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            # n>0 + low timeout so the generator actually runs the
            # prelude (yielding nothing because the stream is empty)
            # and lands in the BLOCK loop's first XREAD, which we
            # then cancel via the timeout. ``n=0`` short-circuits
            # through ``aclose`` without ever entering the generator.
            frames = await _collect_n_frames(gen, n=1, timeout=0.3)

        assert frames == []
        assert prelude.await_count == 1
        assert idle_xread.await_count >= 1
        # No prelude advance → BLOCK loop cursor stays ``$``.
        assert idle_xread.await_args_list[0].args[0] == {f"meho:feed:{_TENANT_A}": "$"}

    async def test_explicit_since_cursor_skips_prelude(
        self,
        _feed_env: None,
    ) -> None:
        """``cursor != "$"`` → prelude is bypassed; only BLOCK loop runs.

        Subscribers passing ``Last-Event-Id`` (after a reconnect) or
        ``since`` (explicit replay) pinned an anchor. Replaying from
        ``+`` would re-deliver entries the caller already saw —
        ``EventSource`` would show duplicates and the operator
        couldn't trust the cursor handshake. The prelude must skip
        on any non-``$`` cursor.
        """
        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        prelude = AsyncMock(return_value=[])
        idle_xread = _idle_xread_mock()
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="1715600000000-0",
                op_class=None,
                principal=None,
                target=None,
            )
            # See the empty-stream test for why n=1 (not 0) is the
            # right driver — ``_collect_n_frames`` short-circuits on
            # ``n<=0`` before the generator runs.
            await _collect_n_frames(gen, n=1, timeout=0.3)

        assert prelude.await_count == 0
        assert idle_xread.await_count >= 1
        assert idle_xread.await_args_list[0].args[0] == {
            f"meho:feed:{_TENANT_A}": "1715600000000-0"
        }

    async def test_prelude_applies_op_class_filter(
        self,
        _feed_env: None,
    ) -> None:
        """Prelude entries that fail ``op_class`` filter are dropped just like live-tail.

        Same ``_process_entries`` helper as the live loop, so
        op_class / principal / target filters work identically.
        """
        read_event = _make_event(op_class="read", op_id="vsphere.vm.list")
        write_event = _make_event(op_class="write", op_id="vsphere.vm.create")
        # XREVRANGE returns latest-first; published order is read then write.
        xrevrange_items = [
            ("1715600000001-0", {"event": write_event.model_dump_json()}),
            ("1715600000000-0", {"event": read_event.model_dump_json()}),
        ]
        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        prelude = AsyncMock(return_value=xrevrange_items)
        idle_xread = _idle_xread_mock()
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class="read",
                principal=None,
                target=None,
            )
            # n=2 reaches the BLOCK loop's first XREAD (only one
            # frame survives the filter, so the second iteration
            # suspends on xread until the timeout).
            frames = await _collect_n_frames(gen, n=2, timeout=0.5)

        assert len(frames) == 1
        data = json.loads(
            next(line for line in frames[0].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert data["op_class"] == "read"
        # Cursor advance is past the latest *fetched* (write) entry,
        # not past the matched (read) entry — mirrors the live-loop
        # busy-but-filtered invariant.
        first_xread_streams = idle_xread.await_args_list[0].args[0]
        assert first_xread_streams == {f"meho:feed:{_TENANT_A}": "1715600000001-0"}

    async def test_prelude_redis_error_emits_feed_error_then_closes(
        self,
        _feed_env: None,
    ) -> None:
        """Transport-down on the prelude XREVRANGE → SSE error frame, clean close.

        Same operator-visible condition as a BLOCK-loop XREAD
        failure (broadcast pod down on a fresh deploy, post-rollout
        connection refused). The prelude is the FIRST Valkey call
        on a fresh ``$`` connection, so the prelude path must also
        emit the T11-compliant ``event: feed_error`` frame rather
        than propagating the exception (which would land as a bare
        connection drop on the SSE consumer, since
        ``http.response.start`` was sent on the StreamingResponse's
        first byte).
        """
        import redis.exceptions as redis_exc

        fast_client = get_broadcast_client()
        blocking_client = get_broadcast_blocking_client()
        prelude = AsyncMock(side_effect=redis_exc.ConnectionError("connection refused"))
        idle_xread = _idle_xread_mock()
        with (
            patch.object(fast_client, "xrevrange", new=prelude),
            patch.object(blocking_client, "xread", new=idle_xread),
        ):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="$",
                op_class=None,
                principal=None,
                target=None,
            )
            frames: list[str] = []
            async for chunk in gen:
                frames.append(chunk)

        assert len(frames) == 1
        assert frames[0].startswith("event: feed_error\n")
        data = json.loads(
            next(line for line in frames[0].split("\n") if line.startswith("data: "))[
                len("data: ") :
            ]
        )
        assert data["code"] == "broadcast_subsystem_unavailable"
        assert "ConnectionError" in data["message"]
        # The BLOCK loop is never reached after the prelude's error
        # frame because the prelude path ``return``s; xread must not
        # have been called.
        assert idle_xread.await_count == 0


# ---------------------------------------------------------------------------
# Cursor resolution
# ---------------------------------------------------------------------------


class TestResolveCursor:
    """``Last-Event-Id`` > ``since`` > ``$`` precedence."""

    def test_header_takes_precedence_over_since(self) -> None:
        from meho_backplane.api.v1.feed import _resolve_cursor

        assert _resolve_cursor("from-header", "from-query") == "from-header"

    def test_since_used_when_no_header(self) -> None:
        from meho_backplane.api.v1.feed import _resolve_cursor

        assert _resolve_cursor(None, "from-query") == "from-query"

    def test_dollar_default_when_neither_set(self) -> None:
        from meho_backplane.api.v1.feed import _resolve_cursor

        assert _resolve_cursor(None, None) == "$"

    def test_empty_header_falls_through_to_since(self) -> None:
        from meho_backplane.api.v1.feed import _resolve_cursor

        # An empty Last-Event-Id header is the "no resumption point"
        # signal — fall through to since rather than passing the
        # empty string to xread (which would be a redis-py argument
        # error).
        assert _resolve_cursor("", "from-query") == "from-query"


# ---------------------------------------------------------------------------
# Real-Valkey integration
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestFeedIntegration:
    """``publish_event`` → ``_feed_generator`` reads it back over real Valkey."""

    @pytest.fixture
    async def valkey_url(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        with RedisContainer(image) as container:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(6379)
            url = f"redis://{host}:{port}"
            monkeypatch.setenv("BROADCAST_REDIS_URL", url)
            monkeypatch.setenv("KEYCLOAK_ISSUER_URL", ISSUER)
            monkeypatch.setenv("KEYCLOAK_AUDIENCE", AUDIENCE)
            monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
            get_settings.cache_clear()
            reset_broadcast_client_for_testing()
            reset_broadcast_blocking_client_for_testing()
            try:
                yield url
            finally:
                await dispose_broadcast_client()
                await dispose_broadcast_blocking_client()
                get_settings.cache_clear()

    async def test_publish_then_generator_read(self, valkey_url: str) -> None:
        """End-to-end: publish_event into Valkey → generator yields back."""
        event = _make_event(tenant_id=_TENANT_A)

        # The generator's cursor must be set to "0" (from-beginning)
        # rather than "$" (live-tail) because the publish happens
        # before the first xread call here — with $ we'd miss the
        # entry (xread interprets $ as "the last id at xread-call
        # time", and the entry was added before that).
        await publish_event(event)

        gen = _feed_generator(
            operator=_make_operator(tenant_id=_TENANT_A),
            cursor="0",
            op_class=None,
            principal=None,
            target=None,
        )
        frames = await _collect_n_frames(gen, n=1, timeout=5.0)

        assert len(frames) == 1
        data_line = next(line for line in frames[0].split("\n") if line.startswith("data: "))
        decoded = json.loads(data_line[len("data: ") :])
        rebuilt = BroadcastEvent.model_validate(decoded)
        assert rebuilt == event
