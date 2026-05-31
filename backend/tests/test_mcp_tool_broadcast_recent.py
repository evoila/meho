# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``meho.broadcast.recent`` (G6.4-T1, #1091).

Acceptance-criteria coverage:

* ``meho.broadcast.recent`` is registered and visible on ``tools/list``
  for an ``operator`` JWT; hidden from ``read_only``.
* Default 30m window when ``since`` is omitted; explicit ``since``
  honoured for both ISO-8601 and Valkey-cursor shapes.
* ``filter.op_class`` / ``filter.principal`` / ``filter.target``
  narrow the result (one test per sub-key).
* ``limit`` is enforced at the input-schema level: out-of-range
  values rejected with ``-32602`` Invalid Params; the in-range
  ``count`` is propagated to ``xrange``.
* Tenant boundary is structural -- the stream key passed to
  ``xrange`` is always ``meho:feed:{operator.tenant_id}``, regardless
  of arguments. The integration test seeds two tenants' streams and
  asserts each operator reads only their own.
* ``next_cursor`` round-trip: feeding the response's ``next_cursor``
  back as ``since`` reaches the next page with no overlap, no gaps
  (validated against a real Valkey container).
* Credential_read + audit_query events surface aggregate-only -- the
  publisher already redacts at write time, so the tool surfaces what's
  on the stream verbatim (no re-redaction, no leakage).

The mocked-client tests cover the wire surface (registration, schema,
validation, handler glue). The Docker-gated ``TestBroadcastRecentIntegration``
suite spins up ``valkey/valkey:8`` via testcontainers and drives the
full publisher -> xrange -> tool-handler seam, including the cursor
round-trip and the two-tenant isolation guarantee.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    BroadcastEvent,
    dispose_broadcast_client,
    get_broadcast_client,
    publish_event,
    reset_broadcast_client_for_testing,
)
from meho_backplane.broadcast.history import (
    InvalidSinceError,
    default_since_ms,
    parse_since,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.tools.broadcast import _handler_recent
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_AUDIT_ID: UUID = UUID("44444444-4444-4444-4444-444444444444")


# ---------------------------------------------------------------------------
# Per-test broadcast-client isolation (mirrors test_mcp_resource_tenant_feed)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the cached client + pin a stub URL around every test.

    Pinning ``BROADCAST_REDIS_URL`` keeps the construction path free of
    "URL not set" failures; per-test patches replace ``xrange`` so no
    socket ever opens.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
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
    """Build a :class:`BroadcastEvent` shaped like one the publisher emits.

    The ``payload`` defaults to the FULL shape the publisher emits for
    non-sensitive ops; sensitive-class tests pass the aggregate shape
    explicitly so the assertion targets the on-stream contract.
    """
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


def _xrange_entry(event: BroadcastEvent, entry_id: str) -> tuple[str, dict[str, str]]:
    """Build one ``XRANGE``-shaped entry tuple from a :class:`BroadcastEvent`."""
    return entry_id, {"event": event.model_dump_json()}


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
def test_tools_list_exposes_recent_for_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` role sees ``meho.broadcast.recent`` on tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.recent" in names
    # MEHO-internal RBAC fields stripped from the wire shape.
    recent_def = next(t for t in body["result"]["tools"] if t["name"] == "meho.broadcast.recent")
    assert "required_role" not in recent_def
    assert "op_class" not in recent_def
    # Schema contract surfaces on the wire.
    schema = recent_def["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert "cursor" in schema["properties"]
    assert "since" in schema["properties"]  # deprecated alias kept for backward compat
    assert "filter" in schema["properties"]
    assert "limit" in schema["properties"]
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 1000
    # Migration nudge: the deprecated alias carries the schema flag.
    assert schema["properties"]["since"].get("deprecated") is True
    assert "deprecated" not in schema["properties"]["cursor"]


def test_tools_list_hides_recent_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``read_only`` operator does NOT see the operator-gated tool."""
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.recent" not in names


def test_read_only_tools_call_recent_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from below ``operator`` rejects with -32602.

    The registry filter hides the tool from ``tools/list``; the
    dispatcher's call-time RBAC re-check is the load-bearing second
    gate against a client that knows the name and posts anyway.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Default 30m window + explicit `since`
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_default_since_uses_30_minute_window(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """No ``since`` -> xrange ``min`` is now - 30 min (bare-ms cursor)."""
    client, op = client_with_operator
    before_ms = int(time.time() * 1000)
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    after_ms = int(time.time() * 1000)

    xr.assert_awaited_once()
    args, kwargs = xr.await_args.args, xr.await_args.kwargs
    assert args[0] == f"meho:feed:{op.tenant_id}"
    assert kwargs["max"] == "+"
    assert kwargs["count"] == 100  # default limit
    min_cursor_ms = int(kwargs["min"])
    # The min cursor must fall in the [before - 30m, after - 30m] window.
    assert before_ms - 30 * 60_000 <= min_cursor_ms <= after_ms - 30 * 60_000


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_since_iso8601_normalised_to_bare_ms(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An ISO-8601 ``since`` is converted to bare-ms (inclusive)."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        post_mcp(
            client,
            _tools_call("meho.broadcast.recent", {"since": "2026-05-25T10:00:00Z"}),
        )
    kwargs = xr.await_args.kwargs
    expected_ms = int(datetime(2026, 5, 25, 10, 0, tzinfo=UTC).timestamp() * 1000)
    assert kwargs["min"] == str(expected_ms)


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_since_cursor_form_becomes_exclusive_lower_bound(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A Valkey-cursor ``since`` is wrapped in ``(`` for exclusive-min.

    This is the load-bearing pagination contract: without the
    exclusive prefix, a next-page call would re-fetch the entry the
    previous page's ``next_cursor`` pointed at.
    """
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        post_mcp(
            client,
            _tools_call("meho.broadcast.recent", {"since": "1747800000000-0"}),
        )
    kwargs = xr.await_args.kwargs
    assert kwargs["min"] == "(1747800000000-0"


def test_since_invalid_iso_rejects_with_domain_error() -> None:
    """A garbled ``since`` raises :class:`InvalidSinceError` at parse time.

    The shared helper raises a domain error rather than the MCP-specific
    :class:`McpInvalidParamsError` (the MCP handler maps the domain
    error to ``-32602`` upstream). The end-to-end MCP wire test for the
    same condition lives at
    :func:`test_invalid_since_iso_returns_invalid_params_at_wire` below.
    """
    with pytest.raises(InvalidSinceError, match="not a valid ISO-8601"):
        parse_since("not-a-real-timestamp")


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_invalid_since_iso_returns_invalid_params_at_wire(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A garbled ``since`` from a wire client surfaces as JSON-RPC -32602.

    The shared helper raises :class:`InvalidSinceError`; the MCP handler
    catches that domain error and re-raises it as
    :class:`McpInvalidParamsError`, which the dispatcher renders as
    ``-32602``. Locks the contract: the helper's split from MCP error
    vocabulary doesn't leak Pyhton exception names to wire clients.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call("meho.broadcast.recent", {"since": "garbled"}),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "since" in body["error"]["message"].lower()


def test_parse_since_default_is_now_minus_30m() -> None:
    """``parse_since(None)`` returns a bare-ms ~30 minutes in the past."""
    before_ms = int(time.time() * 1000)
    cursor = parse_since(None)
    after_ms = int(time.time() * 1000)
    assert cursor.isdigit()
    cursor_ms = int(cursor)
    assert before_ms - 30 * 60_000 <= cursor_ms <= after_ms - 30 * 60_000


def test_default_since_ms_handles_clock_underflow(monkeypatch: pytest.MonkeyPatch) -> None:
    """A clock skew producing a negative bare-ms clamps to 0.

    Documented contract; without the clamp, a misconfigured clock
    would send a negative ``min`` to Valkey and the server would
    reject the command, breaking every call until the clock recovers.
    """
    monkeypatch.setattr(time, "time", lambda: 60.0)  # ts*1000 = 60_000 ms; minus 30m = -ve
    assert default_since_ms() == 0


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
    client, _op = client_with_operator
    read_event = _make_event(op_id="vsphere.vm.list", op_class="read")
    write_event = _make_event(op_id="vsphere.vm.create", op_class="write")
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(
            return_value=[
                _xrange_entry(read_event, "1747800000000-0"),
                _xrange_entry(write_event, "1747800001000-0"),
            ],
        ),
    ):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.recent", {"filter": {"op_class": "write"}}),
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
    client, _op = client_with_operator
    alice = _make_event(op_id="vsphere.vm.list", principal_sub="op-alice")
    bob = _make_event(op_id="vsphere.vm.list", principal_sub="op-bob")
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(
            return_value=[
                _xrange_entry(alice, "1747800000000-0"),
                _xrange_entry(bob, "1747800001000-0"),
            ],
        ),
    ):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.recent", {"filter": {"principal": "op-alice"}}),
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
    client, _op = client_with_operator
    prod = _make_event(target_name="prod-vc-1")
    staging = _make_event(target_name="staging-vc-1")
    untagged = _make_event(target_name=None)
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(
            return_value=[
                _xrange_entry(prod, "1747800000000-0"),
                _xrange_entry(staging, "1747800001000-0"),
                _xrange_entry(untagged, "1747800002000-0"),
            ],
        ),
    ):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.recent", {"filter": {"target": "prod-vc-1"}}),
        )
    result = _result_dict(resp)
    targets = [e["target_name"] for e in result["events"]]
    assert targets == ["prod-vc-1"]


# ---------------------------------------------------------------------------
# limit cap + invalid limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_explicit_limit_propagates_to_xrange_count(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """An in-range ``limit`` is passed through to ``xrange(count=...)``."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        post_mcp(client, _tools_call("meho.broadcast.recent", {"limit": 250}))
    assert xr.await_args.kwargs["count"] == 250


@pytest.mark.parametrize(
    "client_with_operator,bad_limit",
    [
        (TenantRole.OPERATOR, 0),
        (TenantRole.OPERATOR, -5),
        (TenantRole.OPERATOR, 1001),
        (TenantRole.OPERATOR, 100_000),
    ],
    indirect=["client_with_operator"],
)
def test_out_of_range_limit_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    bad_limit: int,
) -> None:
    """``limit`` outside [1, 1000] rejects with JSON-RPC -32602.

    The dispatcher's JSON-Schema validator runs against ``inputSchema``
    before the handler -- a value below ``minimum`` or above
    ``maximum`` surfaces as Invalid Params with the schema error
    embedded in the message.
    """
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho.broadcast.recent", {"limit": bad_limit}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_non_integer_limit_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``limit`` of the wrong JSON type rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho.broadcast.recent", {"limit": "100"}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


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
    another tenant's stream. Asserting the read key is the right
    shape proves the structural property holds.
    """
    client, op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    assert xr.await_args.args[0] == f"meho:feed:{op.tenant_id}"


async def test_handler_with_distinct_operator_reads_distinct_stream() -> None:
    """Two operators on different tenants read their own streams only.

    Directly exercises the handler (bypassing the FastAPI dispatcher)
    with two operators bound to two different tenant ids; asserts each
    call resolves to its own stream key. The mocked client always
    returns ``[]`` -- the assertion is on the key passed to xrange,
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

    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])) as xr:
        await _handler_recent(op_a, {})
        await _handler_recent(op_b, {})

    keys_read = [call.args[0] for call in xr.await_args_list]
    assert keys_read == [f"meho:feed:{tenant_a}", f"meho:feed:{tenant_b}"]


# ---------------------------------------------------------------------------
# next_cursor shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_next_cursor_is_last_fetched_entry_id_when_page_full(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A full page returns the last *fetched* entry id as ``next_cursor``.

    The cursor is the last entry id from the xrange response (NOT the
    last matched-after-filter id), so a page where every entry was
    filtered out still produces a non-null cursor and the caller can
    keep walking without an infinite-loop risk.
    """
    client, _op = client_with_operator
    entries = [
        _xrange_entry(_make_event(op_id="vsphere.vm.list"), f"17478{i:08d}-0") for i in range(3)
    ]
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=entries)):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {"limit": 3}))
    result = _result_dict(resp)
    assert result["next_cursor"] == "1747800000002-0"


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_next_cursor_is_null_when_short_page(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A page shorter than ``limit`` returns ``next_cursor=null``.

    Signals "you've reached the live tail"; the caller can stop
    paginating until new events arrive.
    """
    client, _op = client_with_operator
    entries = [_xrange_entry(_make_event(), "1747800000000-0")]
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=entries)):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {"limit": 10}))
    result = _result_dict(resp)
    assert result["next_cursor"] is None


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_empty_stream_returns_empty_events_null_cursor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Empty stream returns ``events=[]`` + ``next_cursor=null``."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=[])):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    result = _result_dict(resp)
    assert result == {"events": [], "next_cursor": None}


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_event_dict_carries_stream_entry_id_and_event_fields(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Each event surfaces both ``id`` (stream cursor) and BroadcastEvent fields.

    ``id`` is the Valkey stream entry id (the cursor a caller
    round-trips); ``event_id`` / ``ts`` / ``audit_id`` / ``payload``
    are the durable fields from :class:`BroadcastEvent`.
    """
    client, op = client_with_operator
    event = _make_event(op_id="vsphere.vm.list")
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(return_value=[_xrange_entry(event, "1747800000000-0")]),
    ):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    result = _result_dict(resp)
    assert len(result["events"]) == 1
    e = result["events"][0]
    assert e["id"] == "1747800000000-0"
    assert e["event_id"] == str(event.event_id)
    assert e["tenant_id"] == str(op.tenant_id)
    assert e["op_id"] == "vsphere.vm.list"
    assert e["audit_id"] == str(_AUDIT_ID)
    assert "ts" in e
    assert "payload" in e


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
    client, _op = client_with_operator
    aggregate_payload = {"op_class": "credential_read", "result_status": "ok"}
    event = _make_event(
        op_id="vault.kv.read",
        op_class="credential_read",
        payload=aggregate_payload,
    )
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(return_value=[_xrange_entry(event, "1747800000000-0")]),
    ):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {}))
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
    client, _op = client_with_operator
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
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xrange",
        new=AsyncMock(return_value=[_xrange_entry(event, "1747800000000-0")]),
    ):
        resp = post_mcp(client, _tools_call("meho.broadcast.recent", {}))
    result = _result_dict(resp)
    assert result["events"][0]["payload"] == aggregate_payload


# ---------------------------------------------------------------------------
# Deserialisation safety net (skip malformed entries, don't raise)
# ---------------------------------------------------------------------------


async def test_handler_skips_unknown_field_shape() -> None:
    """An entry without an ``event`` field is logged + skipped, not raised."""
    op = build_operator(TenantRole.OPERATOR)
    good = _make_event()
    raw_entries = [
        ("1747800000000-0", {"unexpected_key": "nothing-useful"}),
        _xrange_entry(good, "1747800001000-0"),
    ]
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=raw_entries)):
        result = await _handler_recent(op, {})
    assert len(result["events"]) == 1
    assert result["events"][0]["event_id"] == str(good.event_id)


async def test_handler_skips_malformed_event_json() -> None:
    """An entry whose ``event`` field doesn't parse as BroadcastEvent is skipped."""
    op = build_operator(TenantRole.OPERATOR)
    good = _make_event()
    raw_entries = [
        ("1747800000000-0", {"event": '{"not": "a BroadcastEvent"}'}),
        _xrange_entry(good, "1747800001000-0"),
    ]
    bc = get_broadcast_client()
    with patch.object(bc, "xrange", new=AsyncMock(return_value=raw_entries)):
        result = await _handler_recent(op, {})
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
        _tools_call("meho.broadcast.recent", {"filter": {"unknown": "value"}}),
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
        _tools_call("meho.broadcast.recent", {"filter": {"op_class": "made_up"}}),
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
class TestBroadcastRecentIntegration:
    """End-to-end suite against a real Valkey container.

    Drives the same publisher (``publish_event``) the production
    publish-on-write hook uses so the integration covers the full
    XADD -> XRANGE -> handler seam, the cursor round-trip, and the
    two-tenant isolation contract.
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
            try:
                yield url
            finally:
                await dispose_broadcast_client()
                get_settings.cache_clear()

    async def test_publish_then_recent_round_trip(self, valkey_url: str) -> None:
        """Publish two events; the handler reads them back oldest-first."""
        op = build_operator(TenantRole.OPERATOR)
        e_old = _make_event(op_id="vsphere.vm.list")
        e_new = _make_event(op_id="vsphere.vm.create")
        await publish_event(e_old)
        await publish_event(e_new)

        result = await _handler_recent(op, {})
        op_ids = [e["op_id"] for e in result["events"]]
        assert op_ids == ["vsphere.vm.list", "vsphere.vm.create"]

    async def test_cursor_round_trip_walks_forward_without_overlap(
        self,
        valkey_url: str,
    ) -> None:
        """``next_cursor`` from page 1, passed as ``since`` to page 2, yields page 2.

        Three events published; ``limit=2`` slices into two pages. The
        cursor returned by page 1 MUST land page 2 starting strictly
        after the page-1 boundary (no overlap, no gap). This is the
        load-bearing pagination invariant the issue's "round-trip"
        criterion gates on.
        """
        op = build_operator(TenantRole.OPERATOR)
        events = [_make_event(op_id=f"vsphere.vm.op{i}") for i in range(3)]
        for event in events:
            await publish_event(event)

        page1 = await _handler_recent(op, {"limit": 2})
        assert [e["op_id"] for e in page1["events"]] == ["vsphere.vm.op0", "vsphere.vm.op1"]
        assert page1["next_cursor"] is not None

        page2 = await _handler_recent(op, {"limit": 2, "since": page1["next_cursor"]})
        assert [e["op_id"] for e in page2["events"]] == ["vsphere.vm.op2"]
        assert page2["next_cursor"] is None  # reached the tail

        # Page1's last event id must NOT recur in page2.
        page1_ids = {e["id"] for e in page1["events"]}
        page2_ids = {e["id"] for e in page2["events"]}
        assert not (page1_ids & page2_ids), "cursor pagination must not double-deliver"

    async def test_two_tenants_see_only_their_own_events(
        self,
        valkey_url: str,
    ) -> None:
        """Two operators on two tenants each read only their own stream.

        The structural tenant guarantee verified end-to-end: tenant-A's
        operator never sees tenant-B's events even when both streams
        sit on the same Valkey instance.
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

        result_a = await _handler_recent(op_a, {})
        result_b = await _handler_recent(op_b, {})

        a_op_ids = [e["op_id"] for e in result_a["events"]]
        b_op_ids = [e["op_id"] for e in result_b["events"]]
        assert a_op_ids == ["tenant-a.op"]
        assert b_op_ids == ["tenant-b.op"]

    async def test_iso8601_since_filters_to_window(
        self,
        valkey_url: str,
    ) -> None:
        """An ISO-8601 ``since`` slices the stream at the wall-clock cutoff."""
        op = build_operator(TenantRole.OPERATOR)
        # Publish three events; sleep between to ensure distinct ms timestamps.
        await publish_event(_make_event(op_id="vsphere.vm.op_before"))
        await _aio_sleep(0.05)
        cutoff = datetime.now(UTC) - timedelta(milliseconds=10)
        await _aio_sleep(0.05)
        await publish_event(_make_event(op_id="vsphere.vm.op_after_1"))
        await publish_event(_make_event(op_id="vsphere.vm.op_after_2"))

        result = await _handler_recent(
            op,
            {"since": cutoff.isoformat().replace("+00:00", "Z")},
        )
        op_ids = [e["op_id"] for e in result["events"]]
        # Cutoff-relative: only the two "after" events surface.
        assert "vsphere.vm.op_before" not in op_ids
        assert "vsphere.vm.op_after_1" in op_ids
        assert "vsphere.vm.op_after_2" in op_ids


async def _aio_sleep(seconds: float) -> None:
    """Async sleep used by the integration suite (kept local for clarity).

    Single-underscore prefix is deliberate: a dunder prefix triggers Python's
    class-level name mangling (``__foo`` → ``_ClassName__foo``) at the call
    site, which made this module-level helper unreachable from
    ``TestBroadcastRecentIntegration`` and raised ``NameError`` in CI before
    the rename. See: https://docs.python.org/3/reference/expressions.html#atom-identifiers
    """
    import asyncio

    await asyncio.sleep(seconds)
