# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``meho_broadcast_recent`` (G6.4-T1 #1091, #3748).

Since #3748 the tool reads the ``agent_announcement`` archive by its
commit-order sequence number, not the Valkey stream. Coverage:

* Registration, role visibility, and input-schema rejections.
* Default 30-minute window; an ISO-8601 ``cursor`` starts the window
  earlier; an old Valkey stream cursor or garbage is ``-32602``.
* ``filter`` narrowing (principal / target / work_ref); the ``op_class``
  enum still reaches the handler, and no announcement matches it.
* ``limit`` bounds and the history page cursor: pages walk forward with
  no overlap, and the separate ``watch_checkpoint`` stays fixed.
* Tenant isolation: rows and cursors of another tenant are never served.
* Row shape: the durable ``event_id``, agent prose wrapped as untrusted
  text, no Valkey ``cursor`` / ``id`` keys.

The archive cursor edge cases (concurrency, gaps, filtered-empty scans)
live in ``tests/test_broadcast_archive_cursor.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.broadcast.history import (
    InvalidSinceError,
    default_since_ms,
    parse_since,
)
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.broadcast import _handler_recent
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text
from tests.broadcast_admission_helpers import seed_announcement
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin a stub broadcast URL; the recent tool must never open a socket."""
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


def _operator(tenant_id: Any) -> Operator:
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


def _result_dict(response: Any) -> dict[str, Any]:
    """Extract the JSON-decoded tool result from a JSON-RPC response."""
    body = response.json()
    assert "error" not in body, body
    return json.loads(body["result"]["content"][0]["text"])


def _ids(result: dict[str, Any]) -> list[str]:
    return [event["event_id"] for event in result["events"]]


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
    """``operator`` role sees ``meho_broadcast_recent`` on tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho_broadcast_recent" in names
    # MEHO-internal RBAC fields stripped from the wire shape.
    recent_def = next(t for t in body["result"]["tools"] if t["name"] == "meho_broadcast_recent")
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
    assert "meho_broadcast_recent" not in names


def test_read_only_tools_call_recent_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from below ``operator`` rejects with -32602.

    The registry filter hides the tool from ``tools/list``; the
    dispatcher's call-time RBAC re-check is the load-bearing second
    gate against a client that knows the name and posts anyway.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, _tools_call("meho_broadcast_recent", {}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Window and cursor parsing
# ---------------------------------------------------------------------------


async def test_default_window_is_the_last_30_minutes() -> None:
    """No ``cursor`` returns rows from the last 30 minutes, oldest first."""
    tenant_id = uuid4()
    now = datetime.now(UTC)
    await seed_announcement(tenant_id, activity="old", ts=now - timedelta(hours=2))
    first, _ = await seed_announcement(tenant_id, activity="a", ts=now - timedelta(minutes=5))
    second, _ = await seed_announcement(tenant_id, activity="b", ts=now)

    result = await _handler_recent(_operator(tenant_id), {})

    assert _ids(result) == [str(first.event_id), str(second.event_id)]
    assert result["next_cursor"] is None
    assert result["watch_checkpoint"].startswith("w1.")
    assert result["gap"] is None


async def test_iso8601_cursor_starts_the_window() -> None:
    """An ISO-8601 ``cursor`` (or the ``since`` alias) starts the window there."""
    tenant_id = uuid4()
    old, _ = await seed_announcement(tenant_id, ts=datetime(2026, 5, 25, 10, 0, tzinfo=UTC))
    await seed_announcement(tenant_id, ts=datetime(2026, 5, 25, 9, 0, tzinfo=UTC))

    for arg in ("cursor", "since"):
        result = await _handler_recent(_operator(tenant_id), {arg: "2026-05-25T09:30:00Z"})
        assert _ids(result) == [str(old.event_id)]


async def test_cursor_and_since_together_are_refused() -> None:
    with pytest.raises(McpInvalidParamsError, match="not both"):
        await _handler_recent(
            _operator(uuid4()), {"cursor": "2026-05-25T09:30:00Z", "since": "2026-05-25T09:30:00Z"}
        )


async def test_old_stream_cursor_is_refused_with_a_hint() -> None:
    """A Valkey stream id is not a reading position any more."""
    with pytest.raises(McpInvalidParamsError, match="no longer accepted"):
        await _handler_recent(_operator(uuid4()), {"cursor": "1747800000000-0"})


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_invalid_cursor_returns_invalid_params_labelled_cursor(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A garbled cursor surfaces as -32602 labelled with the canonical arg.

    The wire label names the canonical ``cursor`` inputSchema property
    (#1358, #2480), whichever alias the caller passed.
    """
    client, _op = client_with_operator
    bad = "550e8400-e29b-41d4-a716-446655440000"

    def message_for(arg: str) -> str:
        body = post_mcp(client, _tools_call("meho_broadcast_recent", {arg: bad})).json()
        assert "error" in body
        assert body["error"]["code"] == INVALID_PARAMS
        return str(body["error"]["message"])

    canonical = message_for("cursor")
    assert canonical.lower().startswith("cursor:")
    assert message_for("since") == canonical


def test_since_invalid_iso_rejects_with_domain_error() -> None:
    """The stream-window parser (UI history path) raises a domain error."""
    with pytest.raises(InvalidSinceError, match="not a valid ISO-8601"):
        parse_since("not-a-real-timestamp")


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


async def test_filter_principal_narrows_result() -> None:
    tenant_id = uuid4()
    alice, _ = await seed_announcement(tenant_id, principal_sub="op-alice")
    await seed_announcement(tenant_id, principal_sub="op-bob")
    result = await _handler_recent(_operator(tenant_id), {"filter": {"principal": "op-alice"}})
    assert _ids(result) == [str(alice.event_id)]


async def test_filter_target_matches_targets_list() -> None:
    tenant_id = uuid4()
    hit, _ = await seed_announcement(tenant_id, targets=["vc-a", "vc-b"])
    await seed_announcement(tenant_id, targets=["vc-c"])
    result = await _handler_recent(_operator(tenant_id), {"filter": {"target": "vc-b"}})
    assert _ids(result) == [str(hit.event_id)]


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.parametrize("op_class", ["checks", "approval", "credential_write", "write"])
def test_filter_op_class_enum_reaches_the_handler(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    op_class: str,
) -> None:
    """Every ``OP_CLASS_ENUM`` value passes the wire schema (#2720, #2731).

    Posted as a full ``tools/call`` because the dispatcher validates the
    advertised ``enum``. Announcements carry no op class, so the archive
    read returns no events for it.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client, _tools_call("meho_broadcast_recent", {"filter": {"op_class": op_class}})
    )
    assert _result_dict(resp)["events"] == []


# ---------------------------------------------------------------------------
# Limit + paging
# ---------------------------------------------------------------------------


async def test_pages_walk_forward_without_overlap_under_one_checkpoint() -> None:
    """``next_cursor`` pages a fixed view; rows stored later are not mixed in."""
    tenant_id = uuid4()
    seeded = [await seed_announcement(tenant_id, activity=f"n{i}") for i in range(5)]
    op = _operator(tenant_id)

    first = await _handler_recent(op, {"limit": 2})
    assert first["next_cursor"] is not None
    assert first["next_cursor"].startswith("h1.")
    await seed_announcement(tenant_id, activity="after the view")
    second = await _handler_recent(op, {"cursor": first["next_cursor"], "limit": 2})
    third = await _handler_recent(op, {"cursor": second["next_cursor"], "limit": 2})

    walked = _ids(first) + _ids(second) + _ids(third)
    assert walked == [str(event.event_id) for event, _seq in seeded]
    assert third["next_cursor"] is None
    assert first["watch_checkpoint"] == second["watch_checkpoint"] == third["watch_checkpoint"]


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
    resp = post_mcp(client, _tools_call("meho_broadcast_recent", {"limit": bad_limit}))
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
    resp = post_mcp(client, _tools_call("meho_broadcast_recent", {"limit": "100"}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# Tenant boundary
# ---------------------------------------------------------------------------


async def test_reads_only_the_operators_tenant() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    mine, _ = await seed_announcement(tenant_a)
    await seed_announcement(tenant_b)
    result = await _handler_recent(_operator(tenant_a), {})
    assert _ids(result) == [str(mine.event_id)]


async def test_cursor_from_another_tenant_is_refused() -> None:
    tenant_a, tenant_b = uuid4(), uuid4()
    for _ in range(3):
        await seed_announcement(tenant_a)
    page = await _handler_recent(_operator(tenant_a), {"limit": 1})
    with pytest.raises(McpInvalidParamsError, match="another tenant"):
        await _handler_recent(_operator(tenant_b), {"cursor": page["next_cursor"]})


# ---------------------------------------------------------------------------
# Row shape
# ---------------------------------------------------------------------------


async def test_row_carries_event_id_and_wrapped_prose_without_stream_ids() -> None:
    tenant_id = uuid4()
    event, _ = await seed_announcement(tenant_id, activity="draining host-7", ttl_minutes=30)
    (row,) = (await _handler_recent(_operator(tenant_id), {}))["events"]
    assert row["event_id"] == str(event.event_id)
    assert row["kind"] == "agent_announcement"
    assert row["activity"] == wrap_untrusted_text("draining host-7")
    assert row["ttl_minutes"] == 30
    assert "cursor" not in row
    assert "id" not in row


# ---------------------------------------------------------------------------
# Schema rejections
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
        _tools_call("meho_broadcast_recent", {"filter": {"unknown": "value"}}),
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
        _tools_call("meho_broadcast_recent", {"filter": {"op_class": "made_up"}}),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
