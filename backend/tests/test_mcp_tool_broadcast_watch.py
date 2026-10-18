# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``meho_broadcast_watch`` (G6.4-T3 #1093, #3748).

Since #3748 the tool watches the ``agent_announcement`` archive: it
scans after the caller's watch checkpoint, waits on the Valkey stream
only as a wake-up, and scans again after every wake-up or timeout.

* Registration, role visibility, argument and ``timeout_ms`` validation.
* Stored announcements after the checkpoint return at once, with the
  checkpoint moved past them; nothing new returns an empty list after
  the wait, with the checkpoint unchanged.
* A wake-up leads to a fresh archive scan.
* When the wake-up fails, the watch polls the archive every
  ``WATCH_POLL_SECONDS`` and still delivers each event once.
* Cancellation during the wait re-raises.
* Checkpoints are tenant-bound; old stream cursors and garbage are
  ``-32602``.
* Real Valkey (Docker-gated): a publish during the wait wakes the
  watcher well before the timeout.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import (
    archive_reader,
    dispose_broadcast_blocking_client,
    dispose_broadcast_client,
    encode_watch_checkpoint,
    publish_agent_announcement,
    reset_broadcast_blocking_client_for_testing,
    reset_broadcast_client_for_testing,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.broadcast import (
    _WATCH_DEFAULT_TIMEOUT_MS,
    _WATCH_MAX_TIMEOUT_MS,
    _WATCH_MIN_TIMEOUT_MS,
    _handler_watch,
    _validate_timeout_ms,
)
from meho_backplane.settings import get_settings
from tests.broadcast_admission_helpers import seed_announcement, trusted_event
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

#: A cursor value for tests that are rejected before any read.
_SINCE_FIXTURE: str = "w1.not-read"


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL and reset the cached clients around every test."""
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    get_settings.cache_clear()


class _FakeWake:
    """Fake Valkey clients for the watch wake-up.

    ``xrevrange`` reports the stream's newest id; ``xread`` is an
    ``AsyncMock`` the test scripts (return ``None`` for a timeout, raise
    for a failed wake-up, or run a side effect that stores a row first).
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.xrevrange = AsyncMock(return_value=[("1700000000000-1", {})])
        self.xread = AsyncMock(return_value=None)
        monkeypatch.setattr(archive_reader, "get_broadcast_client", lambda: self)
        monkeypatch.setattr(archive_reader, "get_broadcast_blocking_client", lambda: self)


@pytest.fixture
def wake(monkeypatch: pytest.MonkeyPatch) -> _FakeWake:
    return _FakeWake(monkeypatch)


def _operator(tenant_id: UUID) -> Operator:
    return Operator(
        sub="reader-1",
        raw_jwt="x",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a ``tools/call`` JSON-RPC envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _ids(result: dict[str, Any]) -> list[str]:
    return [event["event_id"] for event in result["events"]]


# ---------------------------------------------------------------------------
# Registration shape + role visibility + argument validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_watch_for_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` role sees ``meho_broadcast_watch`` on tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho_broadcast_watch" in names
    # MEHO-internal RBAC fields stripped from the wire shape.
    watch_def = next(t for t in body["result"]["tools"] if t["name"] == "meho_broadcast_watch")
    assert "required_role" not in watch_def
    assert "op_class" not in watch_def
    # Schema contract surfaces on the wire.
    schema = watch_def["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "cursor" in schema["properties"]
    assert "since_cursor" in schema["properties"]  # deprecated alias
    assert "filter" in schema["properties"]
    assert "timeout_ms" in schema["properties"]
    # NOTE: `anyOf` is stripped from the wire copy by
    # ``_wire_safe_input_schema`` (Anthropic Messages API rejects
    # top-level combinators). The handler-side XOR enforcement runs
    # against the full ``defn.inputSchema`` and is covered by
    # ``test_handler_rejects_both_cursor_and_since_cursor`` below;
    # ``tests/test_mcp_tools_list_shape_conventions.py`` asserts the
    # internal-shape ``anyOf`` directly off the registry.
    assert "anyOf" not in schema, "wire shape must not expose top-level anyOf"
    assert "required" not in schema
    # The deprecated alias carries the `deprecated: true` flag so a
    # schema-driven client renders the migration nudge.
    assert schema["properties"]["since_cursor"].get("deprecated") is True
    assert "deprecated" not in schema["properties"]["cursor"]
    # ``timeout_ms`` bounds are wired.
    assert schema["properties"]["timeout_ms"]["minimum"] == _WATCH_MIN_TIMEOUT_MS
    assert schema["properties"]["timeout_ms"]["maximum"] == _WATCH_MAX_TIMEOUT_MS
    assert schema["properties"]["timeout_ms"]["default"] == _WATCH_DEFAULT_TIMEOUT_MS


def test_tools_list_hides_watch_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``read_only`` operator does NOT see the operator-gated tool."""
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho_broadcast_watch" not in names


def test_read_only_tools_call_watch_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from below ``operator`` rejects with -32602.

    The registry filter hides the tool from ``tools/list``; the
    dispatcher's call-time RBAC re-check is the load-bearing second
    gate against a client that knows the name and posts anyway.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, _tools_call("meho_broadcast_watch", {"since_cursor": _SINCE_FIXTURE}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# since_cursor required (schema + handler enforcement)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_missing_since_cursor_rejects_with_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Omitting ``since_cursor`` surfaces as JSON-RPC -32602.

    The dispatcher's JSON Schema validator catches the ``required`` violation
    before the handler runs; either path is acceptable as long as the wire
    error is INVALID_PARAMS.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho_broadcast_watch", {}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_empty_since_cursor_rejects_with_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An empty ``since_cursor`` rejects -- the schema's ``minLength: 1`` guards it."""
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho_broadcast_watch", {"since_cursor": ""}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


async def test_handler_directly_rejects_non_string_since_cursor() -> None:
    """Handler-side typed-contract: non-string cursor raises -32602.

    Belt-and-suspenders over the schema -- a future schema relaxation
    wouldn't silently widen what the handler accepts. Post-G0.18-T5
    #1358 the error message names ``cursor`` (the canonical wire
    name); both alias names route through the same handler reject
    branch.
    """
    op = build_operator(TenantRole.OPERATOR)
    with pytest.raises(McpInvalidParamsError, match="cursor"):
        await _handler_watch(op, {"since_cursor": 12345})
    with pytest.raises(McpInvalidParamsError, match="cursor"):
        await _handler_watch(op, {"cursor": 12345})


async def test_handler_rejects_both_cursor_and_since_cursor() -> None:
    """Passing both alias names rejects with -32602 (G0.18-T5 #1358).

    The wire layer accepts both spellings to ease the v0.8.0 → v0.9.0
    migration, but exactly one must be supplied per call — passing both
    is an unambiguous client bug that surfaces at the handler boundary.
    """
    op = build_operator(TenantRole.OPERATOR)
    with pytest.raises(McpInvalidParamsError, match=r"cursor.*since_cursor"):
        await _handler_watch(
            op,
            {"cursor": _SINCE_FIXTURE, "since_cursor": _SINCE_FIXTURE},
        )


async def test_handler_rejects_neither_cursor_nor_since_cursor() -> None:
    """Supplying neither cursor spelling rejects with -32602 at the handler (#2644).

    The registered ``inputSchema``'s top-level ``anyOf`` catches this on the
    MCP wire (``test_missing_since_cursor_rejects_with_invalid_params``), but
    that combinator is stripped from every *published* schema — the MCP
    ``tools/list`` copy and the agent bridge's copy alike — because the
    Anthropic Messages API rejects it. The handler is therefore the surface
    that has to reject the both-or-neither forms wherever no jsonschema layer
    runs first, which is exactly the hosted-agent path
    (``pydantic_ai.Tool.from_schema`` does not validate arguments against the
    advertised schema).
    """
    op = build_operator(TenantRole.OPERATOR)
    with pytest.raises(McpInvalidParamsError, match="cursor"):
        await _handler_watch(op, {})
    with pytest.raises(McpInvalidParamsError, match="cursor"):
        await _handler_watch(op, {"timeout_ms": _WATCH_DEFAULT_TIMEOUT_MS})


# ---------------------------------------------------------------------------
# timeout_ms cap (schema + handler validation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator,bad_timeout",
    [
        (TenantRole.OPERATOR, 0),  # infinite-block not allowed
        (TenantRole.OPERATOR, 99),  # below 100ms floor
        (TenantRole.OPERATOR, -5),  # negative
        (TenantRole.OPERATOR, 30_001),  # one ms past the cap
        (TenantRole.OPERATOR, 60_000),  # well past
        (TenantRole.OPERATOR, 1_000_000),  # absurd
    ],
    indirect=["client_with_operator"],
)
def test_out_of_range_timeout_ms_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    bad_timeout: int,
) -> None:
    """``timeout_ms`` outside [100, 30000] rejects with JSON-RPC -32602.

    The dispatcher's JSON-Schema validator runs against ``inputSchema``
    before the handler -- a value below ``minimum`` or above ``maximum``
    surfaces as Invalid Params. Explicitly covers the ``block=0`` case
    (infinite-block) which would otherwise tie up the chassis worker
    until process death.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho_broadcast_watch",
            {"since_cursor": _SINCE_FIXTURE, "timeout_ms": bad_timeout},
        ),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_non_integer_timeout_ms_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``timeout_ms`` of the wrong JSON type rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho_broadcast_watch",
            {"since_cursor": _SINCE_FIXTURE, "timeout_ms": "5000"},
        ),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


def test_handler_timeout_validator_rejects_bool() -> None:
    """``bool`` is a subclass of ``int`` in Python; the validator rejects it explicitly.

    Without the explicit ``bool`` rejection, a caller could pass
    ``timeout_ms=True`` (interpreted as 1ms) or ``timeout_ms=False`` (0ms)
    and silently degrade the long-poll into a non-blocking poll or
    instant-return. The validator catches both.
    """
    with pytest.raises(McpInvalidParamsError, match="timeout_ms"):
        _validate_timeout_ms(True)
    with pytest.raises(McpInvalidParamsError, match="timeout_ms"):
        _validate_timeout_ms(False)


def test_handler_timeout_validator_returns_default_for_none() -> None:
    """``None`` -> the documented default of 10000ms."""
    assert _validate_timeout_ms(None) == _WATCH_DEFAULT_TIMEOUT_MS


@pytest.mark.parametrize(
    "good_timeout",
    [_WATCH_MIN_TIMEOUT_MS, 500, 10_000, _WATCH_MAX_TIMEOUT_MS],
)
def test_handler_timeout_validator_accepts_in_range(good_timeout: int) -> None:
    """Boundary + interior in-range values pass through unchanged."""
    assert _validate_timeout_ms(good_timeout) == good_timeout


# ---------------------------------------------------------------------------
# No events within timeout -> {events: [], next_cursor: unchanged}
# ---------------------------------------------------------------------------
# Archive scans and the wake-up
# ---------------------------------------------------------------------------


async def test_stored_events_after_the_checkpoint_return_at_once(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    _, seen_seq = await seed_announcement(tenant_id)
    newer, newer_seq = await seed_announcement(tenant_id)
    checkpoint = encode_watch_checkpoint(tenant_id, seen_seq)

    result = await _handler_watch(_operator(tenant_id), {"cursor": checkpoint})

    assert _ids(result) == [str(newer.event_id)]
    assert result["next_cursor"] == encode_watch_checkpoint(tenant_id, newer_seq)
    assert result["gap"] is None
    wake.xread.assert_not_awaited()


async def test_nothing_new_waits_then_returns_the_same_checkpoint(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    _, seq = await seed_announcement(tenant_id)
    checkpoint = encode_watch_checkpoint(tenant_id, seq)

    result = await _handler_watch(
        _operator(tenant_id), {"cursor": checkpoint, "timeout_ms": _WATCH_MIN_TIMEOUT_MS}
    )

    assert result == {"events": [], "next_cursor": checkpoint, "gap": None}
    wake.xread.assert_awaited()
    streams = wake.xread.await_args.args[0]
    assert streams == {f"meho:feed:{tenant_id}": "1700000000000-1"}
    assert 1 <= wake.xread.await_args.kwargs["block"] <= _WATCH_MIN_TIMEOUT_MS


async def test_a_wake_up_rescans_the_archive(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    checkpoint = encode_watch_checkpoint(tenant_id, 0)
    stored: list[str] = []

    async def _publish_then_wake(*_args: Any, **_kwargs: Any) -> Any:
        event, _ = await seed_announcement(tenant_id, activity="late")
        stored.append(str(event.event_id))
        return [["key", [("1700000000000-2", {})]]]

    wake.xread.side_effect = _publish_then_wake
    result = await _handler_watch(_operator(tenant_id), {"cursor": checkpoint, "timeout_ms": 5000})
    assert _ids(result) == stored


async def test_filtered_out_rows_still_move_the_checkpoint(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    await seed_announcement(tenant_id, principal_sub="op-bob")
    _, last_seq = await seed_announcement(tenant_id, principal_sub="op-bob")
    op = _operator(tenant_id)
    args = {"filter": {"principal": "op-alice"}, "timeout_ms": _WATCH_MIN_TIMEOUT_MS}

    result = await _handler_watch(op, {"cursor": encode_watch_checkpoint(tenant_id, 0), **args})

    assert result["events"] == []
    assert result["next_cursor"] == encode_watch_checkpoint(tenant_id, last_seq)


async def test_failed_wake_up_polls_and_delivers_each_event_once(
    wake: _FakeWake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Valkey failure falls back to a bounded poll; reconnecting never repeats events."""
    tenant_id = uuid4()
    wake.xread.side_effect = RedisConnectionError("valkey down")
    naps: list[float] = []
    stored: list[str] = []

    async def _nap(seconds: float) -> None:
        naps.append(seconds)
        event, _ = await seed_announcement(tenant_id, activity="while polling")
        stored.append(str(event.event_id))

    monkeypatch.setattr(archive_reader.asyncio, "sleep", _nap)
    op = _operator(tenant_id)

    first = await _handler_watch(
        op, {"cursor": encode_watch_checkpoint(tenant_id, 0), "timeout_ms": 30_000}
    )
    assert _ids(first) == stored
    assert naps == [archive_reader.WATCH_POLL_SECONDS]

    wake.xrevrange.side_effect = RedisConnectionError("valkey down")
    second = await _handler_watch(op, {"cursor": first["next_cursor"], "timeout_ms": 30_000})
    assert _ids(second) == stored[1:]
    assert not set(_ids(first)) & set(_ids(second))
    assert naps[1:] == [archive_reader.WATCH_POLL_SECONDS]


async def test_cancellation_during_the_wait_re_raises(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    wake.xread.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await _handler_watch(
            _operator(tenant_id),
            {"cursor": encode_watch_checkpoint(tenant_id, 0), "timeout_ms": 5000},
        )


# ---------------------------------------------------------------------------
# Checkpoint checks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cursor", "message"),
    [
        ("1747800000000-0", "not a watch checkpoint"),
        ("w1.@@@", "malformed"),
        ("h1.AAAA", "not a watch checkpoint"),
    ],
)
async def test_bad_checkpoints_are_refused(cursor: str, message: str) -> None:
    with pytest.raises(McpInvalidParamsError, match=message):
        await _handler_watch(_operator(uuid4()), {"cursor": cursor})


async def test_checkpoint_from_another_tenant_is_refused() -> None:
    checkpoint = encode_watch_checkpoint(uuid4(), 0)
    with pytest.raises(McpInvalidParamsError, match="another tenant"):
        await _handler_watch(_operator(uuid4()), {"cursor": checkpoint})


async def test_checkpoint_ahead_of_the_archive_resets(wake: _FakeWake) -> None:
    tenant_id = uuid4()
    _, seq = await seed_announcement(tenant_id)
    result = await _handler_watch(
        _operator(tenant_id), {"cursor": encode_watch_checkpoint(tenant_id, seq + 50)}
    )
    assert result["gap"]["reason"] == "checkpoint_ahead"
    assert result["next_cursor"] == encode_watch_checkpoint(tenant_id, seq)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_filter_with_unknown_sub_key_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``filter`` rejects unknown sub-keys via ``additionalProperties: false``."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho_broadcast_watch",
            {"since_cursor": _SINCE_FIXTURE, "filter": {"unknown": "value"}},
        ),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_filter_op_class_outside_enum_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``filter.op_class`` outside the enum rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho_broadcast_watch",
            {"since_cursor": _SINCE_FIXTURE, "filter": {"op_class": "made_up"}},
        ),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_unknown_top_level_argument_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``additionalProperties: false`` at the top level rejects stray keys.

    Defends against a future-caller typo (e.g. ``tenant_id`` smuggled in
    as a hopeful escape hatch) -- the schema rejects it before the
    handler runs.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho_broadcast_watch",
            {"since_cursor": _SINCE_FIXTURE, "tenant_id": "00000000-0000-0000-0000-000000000bad"},
        ),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Optional testcontainers integration suite
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _docker_socket_present(), reason=_SKIP_REASON)
class TestBroadcastWatchIntegration:
    """The stream wake-up against a real Valkey container."""

    @pytest.fixture
    async def valkey_url(self, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[str]:
        from testcontainers.redis import RedisContainer

        image = os.environ.get("MEHO_TEST_VALKEY_IMAGE", "valkey/valkey:8")
        with RedisContainer(image) as container:
            host = container.get_container_host_ip()
            port = container.get_exposed_port(6379)
            url = f"redis://{host}:{port}"
            monkeypatch.setenv("BROADCAST_REDIS_URL", url)
            get_settings.cache_clear()
            reset_broadcast_client_for_testing()
            reset_broadcast_blocking_client_for_testing()
            try:
                yield url
            finally:
                await dispose_broadcast_client()
                await dispose_broadcast_blocking_client()
                get_settings.cache_clear()

    async def test_publish_during_the_wait_wakes_the_watcher(self, valkey_url: str) -> None:
        tenant_id = uuid4()
        op = _operator(tenant_id)
        late = trusted_event(tenant_id=tenant_id, activity="late")

        async def _delayed_publish() -> None:
            await asyncio.sleep(0.2)
            await publish_agent_announcement(late)

        publish_task = asyncio.create_task(_delayed_publish())
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            result = await _handler_watch(
                op, {"cursor": encode_watch_checkpoint(tenant_id, 0), "timeout_ms": 20_000}
            )
        finally:
            await publish_task
        assert _ids(result) == [str(late.event_id)]
        assert loop.time() - started < archive_reader.WATCH_POLL_SECONDS
