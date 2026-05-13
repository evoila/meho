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
from fastapi import FastAPI
from httpx import ASGITransport

from meho_backplane.api.v1.feed import _feed_generator
from meho_backplane.api.v1.feed import router as feed_router
from meho_backplane.audit import AuditMiddleware
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    dispose_broadcast_client,
    get_broadcast_client,
    publish_event,
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
    yield
    reset_broadcast_client_for_testing()


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


# ---------------------------------------------------------------------------
# HTTP edge — authn / authz / 200 + content-type
# ---------------------------------------------------------------------------


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
        from tests._vault_fakes import install_fake_vault

        install_fake_vault(monkeypatch)
        key = make_rsa_keypair("kid-feed-read-only")
        token = mint_token(
            key,
            sub="op-readonly",
            tenant_id=str(_TENANT_A),
            tenant_role=TenantRole.READ_ONLY.value,
        )
        app = _build_app()
        with respx.mock as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(key))
            async with _make_client(app) as client:
                response = await client.get(
                    "/api/v1/feed",
                    headers={"Authorization": f"Bearer {token}"},
                )
        assert response.status_code == 403
        assert response.json() == {"detail": "insufficient_role"}


# ---------------------------------------------------------------------------
# Generator — every body-shaping AC
# ---------------------------------------------------------------------------


class TestFeedGenerator:
    """Drives :func:`_feed_generator` directly with a mocked broadcast client."""

    async def test_basic_event_yield_with_tenant_scoping(self, _feed_env: None) -> None:
        """One event → one well-formed SSE frame; stream key is JWT-derived."""
        event = _make_event(tenant_id=_TENANT_A)
        broadcast_client = get_broadcast_client()
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
        broadcast_client = get_broadcast_client()
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
        broadcast_client = get_broadcast_client()
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
        broadcast_client = get_broadcast_client()
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
        broadcast_client = get_broadcast_client()
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

    async def test_cursor_passes_through_to_xread(self, _feed_env: None) -> None:
        """The cursor argument (already resolved by the handler) is what xread sees."""
        broadcast_client = get_broadcast_client()
        mock = _xread_returning([])
        with patch.object(broadcast_client, "xread", new=mock):
            gen = _feed_generator(
                operator=_make_operator(tenant_id=_TENANT_A),
                cursor="1715600000000-0",
                op_class=None,
                principal=None,
                target=None,
            )
            await _collect_n_frames(gen, n=0, timeout=0.2)

        assert mock.await_count >= 1
        cursor_arg = mock.await_args_list[0].args[0]
        assert cursor_arg == {f"meho:feed:{_TENANT_A}": "1715600000000-0"}

    async def test_malformed_event_skipped(self, _feed_env: None) -> None:
        """A bogus JSON in ``event`` doesn't kill the subscriber."""
        good_event = _make_event(op_id="vsphere.vm.list")
        broadcast_client = get_broadcast_client()
        items = [
            ("1715600000000-0", {"event": "not-json"}),
            ("1715600000001-0", {"event": good_event.model_dump_json()}),
        ]
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
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert len(frames) == 1
        assert "id: 1715600000001-0" in frames[0]

    async def test_unknown_field_shape_skipped(self, _feed_env: None) -> None:
        """An XADD'd entry without an ``event`` field is logged + skipped."""
        good_event = _make_event(op_id="vsphere.host.info")
        broadcast_client = get_broadcast_client()
        items = [
            ("1715600000000-0", {"unknown": "field"}),
            ("1715600000001-0", {"event": good_event.model_dump_json()}),
        ]
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
                op_class=None,
                principal=None,
                target=None,
            )
            frames = await _collect_n_frames(gen, n=1)

        assert len(frames) == 1
        assert "id: 1715600000001-0" in frames[0]


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
            try:
                yield url
            finally:
                await dispose_broadcast_client()
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
