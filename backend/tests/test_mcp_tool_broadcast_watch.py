# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``meho.broadcast.watch`` (G6.4-T3, #1093).

Acceptance-criteria coverage:

* ``meho.broadcast.watch`` is registered and visible on ``tools/list``
  for an ``operator`` JWT; hidden from ``read_only``.
* ``since_cursor`` is required at the schema layer -- missing surfaces as
  JSON-RPC ``-32602`` Invalid Params.
* ``timeout_ms`` is enforced at the input-schema AND handler layers:
  values outside ``[100, 30000]`` reject with ``-32602``. The 30s cap is
  the hard backpressure boundary; ``block=0`` (infinite) is not allowed.
* No events within the block window returns
  ``{events: [], next_cursor: <since_cursor>}`` (unchanged cursor signals
  "I waited; nothing landed").
* New events matching the filter arrive mid-block -> returned immediately
  with ``next_cursor`` advanced to the **last fetched** entry id
  (mirrors the SSE generator's M2 fix: a filter-heavy page still moves
  the cursor forward).
* Tenant boundary is structural -- the stream key passed to ``xread``
  is always ``meho:feed:{operator.tenant_id}``, regardless of arguments.
* ``filter.op_class`` / ``filter.principal`` / ``filter.target`` narrow
  the result (one test per sub-key).
* Cancellation: when Starlette cancels the request mid-block, the handler
  raises :class:`asyncio.CancelledError` (chassis worker released).
* Credential_read + audit_query events surface aggregate-only -- the
  publisher already redacts at write time, so the tool surfaces what's
  on the stream verbatim (no re-redaction, no leakage).

The mocked-client tests cover the wire surface (registration, schema,
validation, handler glue). The Docker-gated
``TestBroadcastWatchIntegration`` suite spins up ``valkey/valkey:8`` via
testcontainers and drives the full publisher -> XREAD -> handler seam,
including the cursor round-trip and the watch-then-arrives cadence.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

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
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_AUDIT_ID: UUID = UUID("55555555-5555-5555-5555-555555555555")

#: A canonical Valkey stream cursor used as ``since_cursor`` in tests
#: that don't exercise cursor-shape semantics directly. The bare-ms +
#: sequence shape matches what :func:`_handler_recent`'s ``next_cursor``
#: would hand the caller in production.
_SINCE_FIXTURE: str = "1747800000000-0"


# ---------------------------------------------------------------------------
# Per-test broadcast-client isolation (mirrors test_mcp_tool_broadcast_recent)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the cached client + pin a stub URL around every test.

    Pinning ``BROADCAST_REDIS_URL`` keeps the construction path free of
    "URL not set" failures; per-test patches replace ``xread`` so no
    socket ever opens.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    reset_broadcast_blocking_client_for_testing()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    tenant_id: UUID = OPERATOR_TENANT_ID,
    op_id: str = "vsphere.vm.list",
    op_class: str = "read",
    principal_sub: str = "op-test",
    target_name: str | None = None,
    result_status: str = "ok",
    payload: dict[str, Any] | None = None,
) -> BroadcastEvent:
    """Build a :class:`BroadcastEvent` shaped like one the publisher emits."""
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 25, 10, 0, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub=principal_sub,
        target_name=target_name,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=_AUDIT_ID,
        payload=payload or {"op_class": op_class, "params": {}, "result_status": result_status},
    )


def _xread_envelope(
    stream_key: str,
    items: list[tuple[BroadcastEvent, str]],
) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    """Build the redis-py ``XREAD`` return shape from event+id tuples.

    redis-py returns ``[[stream_key, [(entry_id, fields), ...]]]``; the
    handler unwraps it via ``entries[0][1]``. *items* pairs each event
    with the stream entry id the publisher would have minted for it.
    """
    fields_list = [(entry_id, {"event": event.model_dump_json()}) for event, entry_id in items]
    return [(stream_key, fields_list)]


def _result_dict(response: Any) -> dict[str, Any]:
    """Extract the JSON-decoded tool result from a JSON-RPC response."""
    body = response.json()
    assert "error" not in body, body
    content = body["result"]["content"]
    return json.loads(content[0]["text"])


def _tools_call(name: str, arguments: dict[str, Any], call_id: int = 1) -> dict[str, Any]:
    """Build a ``tools/call`` JSON-RPC envelope."""
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


# ---------------------------------------------------------------------------
# Registration shape + role visibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_watch_for_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` role sees ``meho.broadcast.watch`` on tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.watch" in names
    # MEHO-internal RBAC fields stripped from the wire shape.
    watch_def = next(t for t in body["result"]["tools"] if t["name"] == "meho.broadcast.watch")
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
    assert "meho.broadcast.watch" not in names


def test_read_only_tools_call_watch_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from below ``operator`` rejects with -32602.

    The registry filter hides the tool from ``tools/list``; the
    dispatcher's call-time RBAC re-check is the load-bearing second
    gate against a client that knows the name and posts anyway.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, _tools_call("meho.broadcast.watch", {"since_cursor": _SINCE_FIXTURE}))
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
    resp = post_mcp(client, _tools_call("meho.broadcast.watch", {}))
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
    resp = post_mcp(client, _tools_call("meho.broadcast.watch", {"since_cursor": ""}))
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
            "meho.broadcast.watch",
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
            "meho.broadcast.watch",
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


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_xread_timeout_none_returns_empty_with_unchanged_cursor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``XREAD`` returning ``None`` (timeout) -> empty + unchanged cursor.

    redis-py's documented "no new messages within block window" shape is
    a ``None`` return value. The handler MUST surface
    ``{events: [], next_cursor: <since_cursor>}`` so the caller knows to
    re-poll with the same cursor.
    """
    client, _op = client_with_operator
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=None)) as xr:
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE, "timeout_ms": 200},
            ),
        )
    result = _result_dict(resp)
    assert result == {"events": [], "next_cursor": _SINCE_FIXTURE}
    # And xread was invoked with the right block window.
    assert xr.await_args.kwargs["block"] == 200


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_xread_empty_list_returns_empty_with_unchanged_cursor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An empty outer list from XREAD is also a "nothing for us" shape.

    Defensive against a future redis-py shape change -- the truthiness
    check collapses ``None`` and ``[]`` into the same branch.
    """
    client, _op = client_with_operator
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=[])):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE},
            ),
        )
    result = _result_dict(resp)
    assert result == {"events": [], "next_cursor": _SINCE_FIXTURE}


# ---------------------------------------------------------------------------
# New events arrive -> returned immediately, cursor advanced
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_new_events_returned_with_advanced_cursor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Entries arriving within the block window surface immediately.

    The handler advances ``next_cursor`` to the LAST FETCHED entry id
    (the M2 invariant: filter-heavy pages still move the cursor forward).
    """
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    e1 = _make_event(op_id="vsphere.vm.list")
    e2 = _make_event(op_id="vsphere.vm.create", op_class="write")
    e3 = _make_event(op_id="vsphere.vm.delete", op_class="write")
    envelope = _xread_envelope(
        stream_key,
        [(e1, "1747800000100-0"), (e2, "1747800000200-0"), (e3, "1747800000300-0")],
    )
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE},
            ),
        )
    result = _result_dict(resp)
    op_ids = [e["op_id"] for e in result["events"]]
    assert op_ids == ["vsphere.vm.list", "vsphere.vm.create", "vsphere.vm.delete"]
    assert result["next_cursor"] == "1747800000300-0"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_cursor_advances_past_filtered_out_entries(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A page where every entry is filtered out still moves the cursor.

    Without this, a busy-but-filtered tenant would loop forever re-reading
    the same XREAD batch on every poll -- the M2 fix the SSE generator
    documents explicitly. ``events`` is empty (filter dropped both), but
    ``next_cursor`` reflects the last *fetched* entry id.
    """
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    read_event = _make_event(op_id="vsphere.vm.list", op_class="read")
    write_event = _make_event(op_id="vsphere.vm.create", op_class="write")
    envelope = _xread_envelope(
        stream_key,
        [(read_event, "1747800000100-0"), (write_event, "1747800000200-0")],
    )
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {
                    "since_cursor": _SINCE_FIXTURE,
                    "filter": {"op_class": "credential_read"},  # matches nothing
                },
            ),
        )
    result = _result_dict(resp)
    assert result["events"] == []
    assert result["next_cursor"] == "1747800000200-0"  # advanced past both


# ---------------------------------------------------------------------------
# Filter narrowing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_filter_op_class_narrows_result(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``filter.op_class`` keeps only matching events."""
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    read_event = _make_event(op_id="vsphere.vm.list", op_class="read")
    write_event = _make_event(op_id="vsphere.vm.create", op_class="write")
    envelope = _xread_envelope(
        stream_key,
        [(read_event, "1747800000100-0"), (write_event, "1747800000200-0")],
    )
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE, "filter": {"op_class": "write"}},
            ),
        )
    result = _result_dict(resp)
    op_ids = [e["op_id"] for e in result["events"]]
    assert op_ids == ["vsphere.vm.create"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_filter_principal_narrows_result(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``filter.principal`` keeps only events from the named JWT 'sub'."""
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    alice = _make_event(op_id="vsphere.vm.list", principal_sub="op-alice")
    bob = _make_event(op_id="vsphere.vm.list", principal_sub="op-bob")
    envelope = _xread_envelope(
        stream_key,
        [(alice, "1747800000100-0"), (bob, "1747800000200-0")],
    )
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE, "filter": {"principal": "op-alice"}},
            ),
        )
    result = _result_dict(resp)
    principals = [e["principal_sub"] for e in result["events"]]
    assert principals == ["op-alice"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_filter_target_narrows_result(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``filter.target`` keeps only events with matching ``target_name``.

    Asserts both:

    1. The matching event survives.
    2. Events with ``target_name=None`` are dropped (the caller asked
       for a specific target; an untagged event doesn't qualify).
    """
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    prod = _make_event(target_name="prod-vc-1")
    staging = _make_event(target_name="staging-vc-1")
    untagged = _make_event(target_name=None)
    envelope = _xread_envelope(
        stream_key,
        [
            (prod, "1747800000100-0"),
            (staging, "1747800000200-0"),
            (untagged, "1747800000300-0"),
        ],
    )
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.watch",
                {"since_cursor": _SINCE_FIXTURE, "filter": {"target": "prod-vc-1"}},
            ),
        )
    result = _result_dict(resp)
    targets = [e["target_name"] for e in result["events"]]
    assert targets == ["prod-vc-1"]


# ---------------------------------------------------------------------------
# Tenant boundary (structural -- no input that could ask for another tenant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_stream_key_derived_from_operator_tenant_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The handler ALWAYS reads ``meho:feed:{operator.tenant_id}``.

    Tenant isolation here is structural: the input schema has no
    ``tenant_id`` field, so a malicious caller has no way to ask for
    another tenant's stream. Asserting the read key is the right shape
    proves the structural property holds.
    """
    client, op = client_with_operator
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=None)) as xr:
        post_mcp(
            client,
            _tools_call("meho.broadcast.watch", {"since_cursor": _SINCE_FIXTURE}),
        )
    streams_arg = xr.await_args.args[0]
    expected_key = f"meho:feed:{op.tenant_id}"
    assert expected_key in streams_arg
    # And the cursor we passed forward is the one the caller sent.
    assert streams_arg[expected_key] == _SINCE_FIXTURE


async def test_handler_with_distinct_operator_reads_distinct_stream() -> None:
    """Two operators on different tenants read their own streams only.

    Directly exercises the handler (bypassing the FastAPI dispatcher)
    with two operators bound to two different tenant ids; asserts each
    call resolves to its own stream key. The mocked client always
    returns ``None`` -- the assertion is on the key passed to xread,
    not on what came back.
    """
    tenant_a = UUID("aaaa0000-0000-0000-0000-000000000001")
    tenant_b = UUID("bbbb0000-0000-0000-0000-000000000002")
    op_a = Operator(
        sub="op-a",
        raw_jwt="x",
        tenant_id=tenant_a,
        tenant_role=TenantRole.OPERATOR,
    )
    op_b = Operator(
        sub="op-b",
        raw_jwt="x",
        tenant_id=tenant_b,
        tenant_role=TenantRole.OPERATOR,
    )

    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=None)) as xr:
        await _handler_watch(op_a, {"since_cursor": _SINCE_FIXTURE})
        await _handler_watch(op_b, {"since_cursor": _SINCE_FIXTURE})

    keys_read = [next(iter(call.args[0].keys())) for call in xr.await_args_list]
    assert keys_read == [f"meho:feed:{tenant_a}", f"meho:feed:{tenant_b}"]


# ---------------------------------------------------------------------------
# Cancellation: client disconnects mid-block -> CancelledError re-raised
# ---------------------------------------------------------------------------


async def test_handler_re_raises_cancelled_error_from_xread() -> None:
    """When ``xread`` raises ``asyncio.CancelledError``, the handler re-raises.

    Swallowing ``CancelledError`` breaks the task tree's unwind invariants
    per the asyncio cancellation contract (Sonar S7497; Python 3.13+
    re-issues cancellation if it goes unpropagated). The handler logs the
    structured disconnect and re-raises so the chassis worker is released
    the moment Starlette cancels the request.
    """
    op = build_operator(TenantRole.OPERATOR)

    async def _cancelled_xread(*_args: object, **_kwargs: object) -> Any:
        raise asyncio.CancelledError

    bc = get_broadcast_blocking_client()
    with (
        patch.object(bc, "xread", new=_cancelled_xread),
        pytest.raises(asyncio.CancelledError),
    ):
        await _handler_watch(op, {"since_cursor": _SINCE_FIXTURE})


async def test_handler_cancellation_during_real_block_releases_worker() -> None:
    """An asyncio-driven cancellation during a real block window propagates cleanly.

    Models the full Starlette cancellation path: a long-poll call is in
    progress, the client transport closes, ``asyncio.Task.cancel()`` lands
    on the awaiting task, and the handler must release the chassis worker
    without swallowing the signal. Drives this with ``asyncio.wait_for``
    against a never-completing fake xread.

    A "release the worker" guarantee that swallowed cancellation would
    fail this test by leaking past the ``asyncio.TimeoutError`` raise --
    that mechanism is exactly the one asyncio uses to escalate a
    cancellation that the awaitee swallows.
    """
    op = build_operator(TenantRole.OPERATOR)

    async def _never_returns(*_args: object, **_kwargs: object) -> Any:
        await asyncio.sleep(3600)  # never completes within the test window
        return None

    bc = get_broadcast_blocking_client()
    with (
        patch.object(bc, "xread", new=_never_returns),
        pytest.raises((asyncio.TimeoutError, asyncio.CancelledError)),
    ):
        await asyncio.wait_for(
            _handler_watch(op, {"since_cursor": _SINCE_FIXTURE}),
            timeout=0.1,
        )


# ---------------------------------------------------------------------------
# PII inheritance: aggregate-only payloads pass through verbatim
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_credential_read_payload_surfaces_aggregate_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``credential_read`` events return whatever the publisher redacted to.

    The tool MUST NOT re-derive or mutate ``payload``; it surfaces the
    on-stream view verbatim. Feeding an aggregate-shaped payload
    asserts the tool doesn't accidentally expand it back to full.
    """
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    aggregate_payload = {"op_class": "credential_read", "result_status": "ok"}
    event = _make_event(
        op_id="vault.kv.read",
        op_class="credential_read",
        payload=aggregate_payload,
    )
    envelope = _xread_envelope(stream_key, [(event, "1747800000100-0")])
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.watch", {"since_cursor": _SINCE_FIXTURE}),
        )
    result = _result_dict(resp)
    assert result["events"][0]["payload"] == aggregate_payload
    # And no path / key / value leak from any earlier-rendered shape.
    assert "params" not in result["events"][0]["payload"]
    assert "path" not in result["events"][0]["payload"]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_audit_query_payload_surfaces_aggregate_with_row_count(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``audit_query`` events surface the ``row_count`` aggregate verbatim."""
    client, op = client_with_operator
    stream_key = f"meho:feed:{op.tenant_id}"
    aggregate_payload = {
        "op_class": "audit_query",
        "result_status": "ok",
        "row_count": 17,
    }
    event = _make_event(
        op_id="meho.audit.replay",
        op_class="audit_query",
        payload=aggregate_payload,
    )
    envelope = _xread_envelope(stream_key, [(event, "1747800000100-0")])
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.watch", {"since_cursor": _SINCE_FIXTURE}),
        )
    result = _result_dict(resp)
    assert result["events"][0]["payload"] == aggregate_payload


# ---------------------------------------------------------------------------
# Deserialisation safety net (skip malformed entries, don't raise)
# ---------------------------------------------------------------------------


async def test_handler_skips_unknown_field_shape() -> None:
    """An entry without an ``event`` field is logged + skipped, not raised."""
    op = build_operator(TenantRole.OPERATOR)
    stream_key = f"meho:feed:{op.tenant_id}"
    good = _make_event()
    envelope = [
        (
            stream_key,
            [
                ("1747800000100-0", {"unexpected_key": "nothing-useful"}),
                ("1747800000200-0", {"event": good.model_dump_json()}),
            ],
        ),
    ]
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        result = await _handler_watch(op, {"since_cursor": _SINCE_FIXTURE})
    assert len(result["events"]) == 1
    assert result["events"][0]["event_id"] == str(good.event_id)
    # Cursor still advances past the malformed entry.
    assert result["next_cursor"] == "1747800000200-0"


async def test_handler_skips_malformed_event_json() -> None:
    """An entry whose ``event`` field doesn't parse as BroadcastEvent is skipped."""
    op = build_operator(TenantRole.OPERATOR)
    stream_key = f"meho:feed:{op.tenant_id}"
    good = _make_event()
    envelope = [
        (
            stream_key,
            [
                ("1747800000100-0", {"event": '{"not": "a BroadcastEvent"}'}),
                ("1747800000200-0", {"event": good.model_dump_json()}),
            ],
        ),
    ]
    bc = get_broadcast_blocking_client()
    with patch.object(bc, "xread", new=AsyncMock(return_value=envelope)):
        result = await _handler_watch(op, {"since_cursor": _SINCE_FIXTURE})
    assert len(result["events"]) == 1
    assert result["events"][0]["event_id"] == str(good.event_id)


# ---------------------------------------------------------------------------
# Filter input-shape validation
# ---------------------------------------------------------------------------


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
            "meho.broadcast.watch",
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
            "meho.broadcast.watch",
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
            "meho.broadcast.watch",
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


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestBroadcastWatchIntegration:
    """End-to-end suite against a real Valkey container.

    Drives the same publisher (``publish_event``) the production
    publish-on-write hook uses so the integration covers the full
    XADD -> XREAD -> handler seam, including the cursor round-trip
    and the watch-then-arrives cadence.
    """

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

    async def test_watch_returns_immediately_when_event_already_present(
        self,
        valkey_url: str,
    ) -> None:
        """Pre-existing entries past the cursor surface on the first call.

        Sets up: publish entry A, get its id; publish entry B; call watch
        with since_cursor=A.id. Expect entry B in the response without
        blocking.
        """
        op = build_operator(TenantRole.OPERATOR)
        event_a = _make_event(op_id="vsphere.vm.list")
        event_b = _make_event(op_id="vsphere.vm.create")
        await publish_event(event_a)
        # Wait so the two events get distinct ids -- Valkey's monotonic
        # ms+seq guarantees ordering, but the cursor round-trip is the
        # contract under test.
        await asyncio.sleep(0.01)
        await publish_event(event_b)

        client = get_broadcast_client()
        stream_key = f"meho:feed:{op.tenant_id}"
        # Get the first event's id by XRANGE on the stream.
        first = await client.xrange(stream_key, count=1)
        assert first, "publisher seeded entry A but XRANGE returned empty"
        a_id = first[0][0]

        result = await _handler_watch(
            op,
            {"since_cursor": a_id, "timeout_ms": 1000},
        )
        op_ids = [e["op_id"] for e in result["events"]]
        assert op_ids == ["vsphere.vm.create"]
        assert result["next_cursor"] != a_id

    async def test_watch_returns_empty_after_timeout_no_events(
        self,
        valkey_url: str,
    ) -> None:
        """No events arrive within the window -> empty + unchanged cursor.

        Uses the smallest allowed timeout to keep the test fast (~100ms).
        """
        op = build_operator(TenantRole.OPERATOR)
        # No publishes -- the stream may not exist yet. XREAD against a
        # missing stream returns the same "no entries" shape.
        result = await _handler_watch(
            op,
            {"since_cursor": "0-0", "timeout_ms": _WATCH_MIN_TIMEOUT_MS},
        )
        assert result == {"events": [], "next_cursor": "0-0"}

    async def test_two_tenants_see_only_their_own_events(
        self,
        valkey_url: str,
    ) -> None:
        """Two operators on two tenants each read only their own stream.

        The structural tenant guarantee verified end-to-end: tenant-A's
        operator never sees tenant-B's events even when both streams sit
        on the same Valkey instance.
        """
        tenant_a = UUID("aaaa0000-0000-0000-0000-000000000001")
        tenant_b = UUID("bbbb0000-0000-0000-0000-000000000002")
        op_a = Operator(
            sub="op-a",
            raw_jwt="x",
            tenant_id=tenant_a,
            tenant_role=TenantRole.OPERATOR,
        )
        op_b = Operator(
            sub="op-b",
            raw_jwt="x",
            tenant_id=tenant_b,
            tenant_role=TenantRole.OPERATOR,
        )

        await publish_event(_make_event(tenant_id=tenant_a, op_id="tenant-a.op"))
        await publish_event(_make_event(tenant_id=tenant_b, op_id="tenant-b.op"))

        # Watch with since_cursor=0-0 to fetch the first batch on each
        # stream regardless of pre-existing data.
        result_a = await _handler_watch(
            op_a,
            {"since_cursor": "0-0", "timeout_ms": _WATCH_MIN_TIMEOUT_MS},
        )
        result_b = await _handler_watch(
            op_b,
            {"since_cursor": "0-0", "timeout_ms": _WATCH_MIN_TIMEOUT_MS},
        )

        a_op_ids = [e["op_id"] for e in result_a["events"]]
        b_op_ids = [e["op_id"] for e in result_b["events"]]
        assert a_op_ids == ["tenant-a.op"]
        assert b_op_ids == ["tenant-b.op"]

    async def test_watch_then_publish_during_block(self, valkey_url: str) -> None:
        """A publish landing mid-block surfaces to the waiting watcher.

        Schedules a publish for ~50ms after the watch starts; the watch
        with a 5s timeout should return well before the timeout fires.
        """
        op = build_operator(TenantRole.OPERATOR)

        async def _delayed_publish() -> None:
            await asyncio.sleep(0.05)
            await publish_event(_make_event(op_id="vsphere.vm.late"))

        publish_task = asyncio.create_task(_delayed_publish())
        try:
            result = await _handler_watch(
                op,
                {"since_cursor": "0-0", "timeout_ms": 5000},
            )
        finally:
            await publish_task

        op_ids = [e["op_id"] for e in result["events"]]
        assert "vsphere.vm.late" in op_ids
