# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the ``GET /api/v1/feed`` SSE endpoint.

Since #3758 the feed streams deliberate agent announcements only, read
from the announcement archive in commit order; the Valkey stream only
wakes the reader. Covers:

* ``GET /api/v1/feed`` with a valid JWT returns 200 +
  ``text/event-stream``; ``read_only`` role → 403; unauthenticated → 401.
* Start resolution: a checkpoint in ``Last-Event-Id`` resumes after it;
  a ``Last-Event-Id`` that is not a checkpoint (a pre-#3758 stream id)
  starts fresh instead of failing; ``since`` takes a checkpoint or an
  ISO-8601 time, anything else is a 400.
* A fresh connection replays the newest announcements, then waits for
  new ones; each frame's ``id`` is a checkpoint a reconnect resumes from.
* Filters, tenant scoping, retention gaps, heartbeats, and the
  ``feed_error`` frame on a database error.
* The audit middleware still forwards SSE chunks live (#1389) and still
  writes the request's audit row (#1599).

Generator tests drive :func:`announcement_feed_frames` directly with a
fake Valkey wake-up: async-generator teardown (``aclose()``) is
deterministic, while ASGI disconnect handling is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport

import meho_backplane.api.v1.feed as feed_module
from meho_backplane.api.v1.feed import (
    FeedStart,
    announcement_feed_frames,
    resolve_feed_start,
)
from meho_backplane.api.v1.feed import router as feed_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    AgentAnnouncementEvent,
    archive_reader,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.archive_cursor import encode_watch_checkpoint
from meho_backplane.broadcast.archive_reader import AnnouncementFilter
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
from tests.broadcast_admission_helpers import seed_announcement

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


def _make_announcement(
    *,
    tenant_id: UUID = _TENANT_A,
    principal_sub: str = "agent-bot",
    activity: str = "rotating tokens on cluster X",
    target: str | None = "cluster-x",
    targets: list[str] | None = None,
    phase: str = "start",
    planned_op_class: str | None = "credential_write",
    ttl_minutes: int | None = 30,
    work_ref: str | None = "gh:evoila/meho#123",
) -> AgentAnnouncementEvent:
    """Build one agent-authored announcement for the union-validation tests."""
    return AgentAnnouncementEvent(
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        activity=activity,
        target=target,
        targets=targets or [],
        scope=None,
        planned_op_class=planned_op_class,  # type: ignore[arg-type]
        ttl_minutes=ttl_minutes,
        work_ref=work_ref,
        phase=phase,  # type: ignore[arg-type]
        ts=datetime(2026, 5, 13, tzinfo=UTC),
    )


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

    async def test_stale_last_event_id_starts_fresh_instead_of_400(
        self,
        _feed_env: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A ``Last-Event-Id`` that is not a checkpoint starts a fresh feed.

        A browser tab open across the #3758 deploy reconnects with an old
        Valkey stream id. A 400 would close its ``EventSource`` for good;
        a fresh start (backlog, then live) keeps the page working.
        """
        starts: list[FeedStart] = []

        async def _finite(_operator: Operator, **kwargs: object) -> AsyncIterator[str]:
            starts.append(kwargs["start"])  # type: ignore[arg-type]
            yield ": heartbeat\n\n"

        monkeypatch.setattr(feed_module, "announcement_feed_frames", _finite)
        response = await _request_feed_authenticated(
            monkeypatch,
            kid="kid-feed-stale-cursor",
            sub="op-stale-cursor",
            role=TenantRole.OPERATOR,
            extra_headers={"Last-Event-Id": "1715600000000-0"},
        )
        assert response.status_code == 200
        assert starts == [FeedStart()]

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

    def test_resolve_feed_start_rules(self) -> None:
        """Checkpoint > ISO ``since`` > fresh; foreign or stale cursors never resume."""
        operator = _make_operator()
        checkpoint = encode_watch_checkpoint(_TENANT_A, 7)
        assert resolve_feed_start(operator, checkpoint, None) == FeedStart(after=7)
        # The header wins over ``since``.
        assert resolve_feed_start(operator, checkpoint, "2026-05-25T10:00:00Z") == FeedStart(
            after=7
        )
        assert resolve_feed_start(operator, None, checkpoint) == FeedStart(after=7)
        expected = datetime(2026, 5, 25, 10, 0, tzinfo=UTC)
        assert resolve_feed_start(operator, None, "2026-05-25T10:00:00Z").since == expected
        # A naive time is UTC; an offset is honoured.
        assert resolve_feed_start(operator, None, "2026-05-25T10:00:00").since == expected
        assert resolve_feed_start(operator, None, "2026-05-25T12:00:00+02:00").since == expected
        assert resolve_feed_start(operator, None, None) == FeedStart()
        # Another tenant's checkpoint in the header starts fresh, never resumes.
        foreign = encode_watch_checkpoint(_TENANT_B, 3)
        assert resolve_feed_start(operator, foreign, None) == FeedStart()
        # Old Valkey ids and bare dates in ``since`` are a 400.
        for bad in ("1715600000000-0", "2026-05-25", foreign):
            with pytest.raises(HTTPException) as exc_info:
                resolve_feed_start(operator, None, bad)
            assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# ASGI-transport e2e — SSE streams incrementally through the full
# middleware stack (regression coverage for #1389)
# ---------------------------------------------------------------------------


def _sse_frame(event: AgentAnnouncementEvent, entry_id: str) -> str:
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

        first_frame = _sse_frame(_make_announcement(), "w1.first")
        first_body_seen = asyncio.Event()
        release = asyncio.Event()

        async def _one_then_wait(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            yield first_frame
            # Suspend until the test confirms the first chunk was
            # forwarded — stands in for the never-returning XREAD BLOCK
            # loop. A buffering middleware never sets ``first_body_seen``,
            # so this wait (and thus the whole request) hangs.
            await release.wait()

        monkeypatch.setattr(feed_module, "announcement_feed_frames", _one_then_wait)

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
            _sse_frame(_make_announcement(activity="one"), "w1.one"),
            _sse_frame(_make_announcement(activity="two"), "w1.two"),
        ]

        async def _finite_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
            for frame in frames:
                yield frame

        monkeypatch.setattr(feed_module, "announcement_feed_frames", _finite_stream)

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

        first_frame = _sse_frame(_make_announcement(), "w1.first")
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

        monkeypatch.setattr(feed_module, "announcement_feed_frames", _one_then_block)

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
# Generator — archive-backed frames (#3758)
# ---------------------------------------------------------------------------


class _FakeWake:
    """Fake Valkey clients for the archive watch wake-up.

    ``xread`` sleeps a little and reports a timeout, so the generator's
    wait loop yields control instead of spinning.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _quiet_xread(*_args: object, **_kwargs: object) -> None:
            await asyncio.sleep(0.01)

        self.xrevrange = AsyncMock(return_value=[("1700000000000-1", {})])
        self.xread = AsyncMock(side_effect=_quiet_xread)
        monkeypatch.setattr(archive_reader, "get_broadcast_client", lambda: self)
        monkeypatch.setattr(archive_reader, "get_broadcast_blocking_client", lambda: self)


@pytest.fixture
def wake(_feed_env: None, monkeypatch: pytest.MonkeyPatch) -> _FakeWake:
    return _FakeWake(monkeypatch)


def _frames(
    *,
    start: FeedStart | None = None,
    flt: AnnouncementFilter | None = None,
    tenant_id: UUID = _TENANT_A,
) -> AsyncIterator[str]:
    return announcement_feed_frames(
        _make_operator(tenant_id=tenant_id),
        start=start or FeedStart(),
        flt=flt or AnnouncementFilter(),
        surface="api",
    )


async def _take(gen: AsyncIterator[str], n: int, timeout: float = 3.0) -> list[str]:
    """Collect up to *n* frames, then close the generator."""
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
        await gen.aclose()  # type: ignore[attr-defined]
    return frames


def _data(frame: str) -> dict[str, object]:
    line = next(part for part in frame.splitlines() if part.startswith("data: "))
    parsed: dict[str, object] = json.loads(line.removeprefix("data: "))
    return parsed


def _frame_id(frame: str) -> str:
    return next(part for part in frame.splitlines() if part.startswith("id: ")).removeprefix("id: ")


class TestFeedGenerator:
    """Frames come from the archive; the stream is only a wake-up."""

    async def test_fresh_connection_replays_stored_announcements(self, wake: _FakeWake) -> None:
        """Backlog from the archive, oldest first, plain (unwrapped) announcement JSON."""
        await seed_announcement(_TENANT_A, activity="first step")
        await seed_announcement(_TENANT_A, activity="second step")

        frames = await _take(_frames(), n=2)

        assert [frame.splitlines()[0] for frame in frames] == ["event: broadcast"] * 2
        assert [_data(frame)["activity"] for frame in frames] == ["first step", "second step"]
        assert {_data(frame)["kind"] for frame in frames} == {"agent_announcement"}

    async def test_backlog_is_the_newest_fifty(self, wake: _FakeWake) -> None:
        for index in range(55):
            await seed_announcement(_TENANT_A, activity=f"step-{index:02d}")

        frames = await _take(_frames(), n=50)

        assert len(frames) == 50
        assert _data(frames[0])["activity"] == "step-05"
        assert _data(frames[-1])["activity"] == "step-54"

    async def test_frame_id_is_a_checkpoint_a_reconnect_resumes_from(self, wake: _FakeWake) -> None:
        """Reconnecting with a frame's id delivers only what came after it."""
        await seed_announcement(_TENANT_A, activity="seen")
        frames = await _take(_frames(), n=1)
        checkpoint = _frame_id(frames[0])
        await seed_announcement(_TENANT_A, activity="missed while away")

        start = resolve_feed_start(_make_operator(), checkpoint, None)
        resumed = await _take(_frames(start=start), n=1)

        assert [_data(frame)["activity"] for frame in resumed] == ["missed while away"]

    async def test_live_announcement_arrives_after_backlog(self, wake: _FakeWake) -> None:
        """An announcement stored while the feed waits is delivered once."""
        gen = _frames()
        task = asyncio.ensure_future(_take(gen, n=1))
        await asyncio.sleep(0.05)
        await seed_announcement(_TENANT_A, activity="new work")
        frames = await task
        assert [_data(frame)["activity"] for frame in frames] == ["new work"]

    async def test_since_time_replays_from_that_time(self, wake: _FakeWake) -> None:
        await seed_announcement(_TENANT_A, activity="old", ts=datetime(2026, 1, 1, tzinfo=UTC))
        await seed_announcement(_TENANT_A, activity="recent")

        start = FeedStart(since=datetime.now(UTC) - timedelta(minutes=5))
        frames = await _take(_frames(start=start), n=2, timeout=1.0)

        assert [_data(frame)["activity"] for frame in frames] == ["recent"]

    async def test_tenant_scoping(self, wake: _FakeWake) -> None:
        """Tenant A's feed never shows tenant B's announcements."""
        await seed_announcement(_TENANT_B, activity="tenant b plan")
        await seed_announcement(_TENANT_A, activity="tenant a plan")

        frames = await _take(_frames(), n=2, timeout=1.0)

        assert [_data(frame)["activity"] for frame in frames] == ["tenant a plan"]

    async def test_principal_and_target_filters(self, wake: _FakeWake) -> None:
        await seed_announcement(_TENANT_A, principal_sub="agent-a", target="cluster-x")
        await seed_announcement(_TENANT_A, principal_sub="agent-b", target="cluster-y")

        by_principal = await _take(
            _frames(flt=AnnouncementFilter(principal="agent-b")), n=2, timeout=1.0
        )
        by_target = await _take(
            _frames(flt=AnnouncementFilter(target="cluster-x")), n=2, timeout=1.0
        )
        by_op_class = await _take(
            _frames(flt=AnnouncementFilter(op_class="write")), n=1, timeout=1.0
        )

        assert [_data(frame)["principal_sub"] for frame in by_principal] == ["agent-b"]
        assert [_data(frame)["target"] for frame in by_target] == ["cluster-x"]
        # ``op_class`` is an operation filter: no announcement matches it.
        assert by_op_class == []

    async def test_heartbeat_on_idle(
        self, wake: _FakeWake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(feed_module, "_HEARTBEAT_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(feed_module, "_WAIT_MS", 20)

        frames = await _take(_frames(), n=1)

        assert frames == [": heartbeat\n\n"]

    async def test_checkpoint_ahead_of_archive_emits_gap_frame(self, wake: _FakeWake) -> None:
        frames = await _take(_frames(start=FeedStart(after=999)), n=1)

        assert frames[0].startswith("event: gap\n")
        assert _data(frames[0])["reason"] == "checkpoint_ahead"

    async def test_database_error_emits_feed_error_then_ends(
        self, wake: _FakeWake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from sqlalchemy.exc import OperationalError

        async def _boom(*_args: object, **_kwargs: object) -> object:
            raise OperationalError("SELECT", {}, Exception("db down"))

        monkeypatch.setattr(feed_module, "latest_announcements", _boom)

        frames = await _take(_frames(), n=5, timeout=1.0)

        assert len(frames) == 1
        assert frames[0].startswith("event: feed_error\n")
        detail = _data(frames[0])
        assert detail["code"] == "broadcast_subsystem_unavailable"
        assert "db down" not in frames[0]

    async def test_valkey_outage_falls_back_to_polling(
        self, wake: _FakeWake, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Valkey failure is not an error: the archive is still read."""
        from redis.exceptions import ConnectionError as RedisConnectionError

        wake.xrevrange.side_effect = RedisConnectionError("valkey down")
        monkeypatch.setattr(archive_reader, "WATCH_POLL_SECONDS", 0.01)
        gen = _frames()
        task = asyncio.ensure_future(_take(gen, n=1))
        await asyncio.sleep(0.05)
        await seed_announcement(_TENANT_A, activity="stored during outage")
        frames = await task

        assert [_data(frame)["activity"] for frame in frames] == ["stored during outage"]
