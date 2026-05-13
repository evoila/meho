# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``meho://tenant/{tenant_id}/feed`` MCP resource (G6.1-T6a, #312).

Covers issue #312's user-facing acceptance criteria:

* **AC #1** — the resource template surfaces via ``resources/templates/list``
  for an operator (and stays hidden from read-only operators).
* **AC #2** — ``resources/read meho://tenant/<own>/feed`` returns the
  last 50 events in chronological order.
* **AC #3** — ``resources/read meho://tenant/<other>/feed`` rejects with
  INVALID_PARAMS (the JSON-RPC -32602 mapping `tenant_info` established
  for the equivalent HTTP 403 case).

AC #4 + AC #5 (50 RPS load test + Valkey-restart chaos) are explicitly
deferred to T7 per the pushback on the issue body; no test in this
file attempts them.

The integration suite at the bottom is Docker-gated: it spins up a
``valkey/valkey:8`` container via testcontainers, XADDs two
:class:`BroadcastEvent`-shaped entries, and asserts the handler reads
them back in chronological order. The mocked-client tests in the body
cover the rejection arms + the deserialisation safety net without
needing Docker.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
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
from meho_backplane.mcp.resources.tenant_feed import _tenant_feed_handler
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

_AUDIT_ID: UUID = UUID("33333333-3333-3333-3333-333333333333")


# ---------------------------------------------------------------------------
# Per-test broadcast-client isolation
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the cached client + pin a stub URL around every test.

    The handler under test calls ``get_broadcast_client()``, which
    instantiates a real :class:`redis.asyncio.Redis` from the
    process-wide ``BROADCAST_REDIS_URL``. Pinning the env var keeps
    the construction path free of "URL not set" failures; the actual
    client's ``xrevrange`` is then patched per-test.
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
    result_status: str = "ok",
) -> BroadcastEvent:
    return BroadcastEvent(
        event_id=uuid4(),
        ts=datetime(2026, 5, 13, tzinfo=UTC),
        tenant_id=tenant_id,
        principal_sub="op-test",
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=_AUDIT_ID,
        payload={"op_class": op_class, "params": {}, "result_status": result_status},
    )


def _xrevrange_entry(event: BroadcastEvent, entry_id: str) -> tuple[str, dict[str, str]]:
    """Build one ``XREVRANGE``-shaped entry tuple from a :class:`BroadcastEvent`."""
    return entry_id, {"event": event.model_dump_json()}


# ---------------------------------------------------------------------------
# resources/templates/list — visibility per role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_templates_list_exposes_tenant_feed_for_operator(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: operator sees the tenant_feed template entry."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "resources/templates/list"},
    )

    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    tenant_feed = [t for t in templates if t["uriTemplate"] == "meho://tenant/{tenant_id}/feed"]
    assert len(tenant_feed) == 1
    assert tenant_feed[0]["mimeType"] == "application/json"
    # MEHO-internal RBAC field stripped from the wire shape.
    assert "required_role" not in tenant_feed[0]


def test_resources_templates_list_hides_tenant_feed_for_read_only(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1 (negative): read-only operator does not see tenant_feed.

    Read-only is below the resource's ``required_role=OPERATOR`` gate,
    so :func:`all_resource_templates_for` filters the entry out before
    the wire shape is built. The ``tenant_info`` resource (required
    role: READ_ONLY) stays visible — proves the filter is per-resource,
    not all-or-nothing.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY

    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 2, "method": "resources/templates/list"},
    )

    assert response.status_code == 200
    body = response.json()
    templates = body["result"]["resourceTemplates"]
    feed_templates = [t for t in templates if t["uriTemplate"].endswith("/feed")]
    assert feed_templates == [], "read_only must not see operator-gated tenant_feed"
    # Read-only IS allowed to see tenant_info — the role filter is per-resource.
    info_templates = [t for t in templates if t["uriTemplate"].endswith("/info")]
    assert len(info_templates) == 1


# ---------------------------------------------------------------------------
# resources/read — happy path through the MCP dispatcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_own_tenant_returns_chronological_events(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #2: read returns the recent events, chronological (oldest-first).

    Patches ``xrevrange`` on the broadcast client to return three
    entries newest-first; asserts the handler reverses them into
    chronological order in the response.
    """
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/feed"

    # Three events; XREVRANGE returns newest-first.
    e_new = _make_event(op_id="vsphere.vm.create")  # newest
    e_mid = _make_event(op_id="vsphere.vm.list")
    e_old = _make_event(op_id="vault.kv.read")  # oldest
    xrev_payload = [
        _xrevrange_entry(e_new, "1715600003000-0"),
        _xrevrange_entry(e_mid, "1715600002000-0"),
        _xrevrange_entry(e_old, "1715600001000-0"),
    ]

    bc = get_broadcast_client()
    with patch.object(bc, "xrevrange", new=AsyncMock(return_value=xrev_payload)) as xrev:
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "resources/read",
                "params": {"uri": uri},
            },
        )

    xrev.assert_awaited_once()
    args = xrev.await_args.args
    kwargs = xrev.await_args.kwargs
    assert args[0] == f"meho:feed:{op.tenant_id}"
    assert kwargs["max"] == "+"
    assert kwargs["min"] == "-"
    assert kwargs["count"] == 50

    assert response.status_code == 200
    body = response.json()
    contents = body["result"]["contents"]
    assert contents[0]["uri"] == uri
    assert contents[0]["mimeType"] == "application/json"

    payload = json.loads(contents[0]["text"])
    assert payload["tenant_id"] == str(op.tenant_id)
    assert payload["count"] == 3
    # Chronological order: oldest first → newest last.
    op_ids = [e["op_id"] for e in payload["events"]]
    assert op_ids == ["vault.kv.read", "vsphere.vm.list", "vsphere.vm.create"]


# ---------------------------------------------------------------------------
# resources/read — rejection arms
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_cross_tenant_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #3: a URI bound to a different tenant rejects with -32602.

    The tenant-boundary check runs *before* the XREVRANGE call, so the
    test doesn't even need to patch ``xrevrange`` — the handler short-
    circuits on the operator-vs-URI mismatch before reaching the
    Valkey layer.
    """
    client, _op = client_with_operator
    foreign_tenant = uuid4()
    uri = f"meho://tenant/{foreign_tenant}/feed"

    bc = get_broadcast_client()
    with patch.object(bc, "xrevrange", new=AsyncMock(return_value=[])) as xrev:
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/read",
                "params": {"uri": uri},
            },
        )

    xrev.assert_not_awaited()  # short-circuited before any Valkey call

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "cross-tenant" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_invalid_uuid_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A non-UUID bound to ``{tenant_id}`` rejects with -32602."""
    client, _op = client_with_operator
    uri = "meho://tenant/not-a-uuid/feed"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "not a uuid" in body["error"]["message"].lower()


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_resources_read_empty_stream_returns_empty_events(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """Empty stream → count=0 + events=[] (never 404 — resource always exists)."""
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/feed"

    bc = get_broadcast_client()
    with patch.object(bc, "xrevrange", new=AsyncMock(return_value=[])):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "resources/read",
                "params": {"uri": uri},
            },
        )

    body = response.json()
    payload = json.loads(body["result"]["contents"][0]["text"])
    assert payload == {
        "tenant_id": str(op.tenant_id),
        "count": 0,
        "events": [],
    }


# ---------------------------------------------------------------------------
# Handler-level: deserialisation safety net (skip + log on bad entry)
# ---------------------------------------------------------------------------


async def test_handler_skips_unknown_field_shape() -> None:
    """An entry without an ``event`` field is logged + skipped, not raised.

    Exercises the safety net at :func:`_parse_entry`: T3's publisher
    always emits ``{"event": <json>}``, but a future Slack-mirror or
    third-party writer could XADD a different shape onto the same
    stream key. The handler must keep going, not 500.
    """
    op = build_operator(TenantRole.OPERATOR)
    # Two entries: one valid (the good one), one with the wrong field shape.
    good = _make_event()
    raw_entries = [
        _xrevrange_entry(good, "1715600002000-0"),
        ("1715600001000-0", {"unexpected_key": "nothing-useful"}),
    ]
    bc = get_broadcast_client()
    with patch.object(bc, "xrevrange", new=AsyncMock(return_value=raw_entries)):
        result = await _tenant_feed_handler(op, {"tenant_id": str(op.tenant_id)})

    # One event survived; the unknown-shape entry got dropped.
    assert result["count"] == 1
    assert result["events"][0]["event_id"] == str(good.event_id)


async def test_handler_skips_malformed_event_json() -> None:
    """An entry whose ``event`` field doesn't parse as ``BroadcastEvent`` is skipped."""
    op = build_operator(TenantRole.OPERATOR)
    good = _make_event()
    raw_entries = [
        _xrevrange_entry(good, "1715600002000-0"),
        ("1715600001000-0", {"event": '{"this": "is not", "a": "BroadcastEvent"}'}),
    ]
    bc = get_broadcast_client()
    with patch.object(bc, "xrevrange", new=AsyncMock(return_value=raw_entries)):
        result = await _tenant_feed_handler(op, {"tenant_id": str(op.tenant_id)})

    assert result["count"] == 1
    assert result["events"][0]["event_id"] == str(good.event_id)


# ---------------------------------------------------------------------------
# Optional testcontainers integration suite — publish-and-read round-trip
# ---------------------------------------------------------------------------


def _docker_socket_present() -> bool:
    return Path("/var/run/docker.sock").exists() or os.environ.get("DOCKER_HOST") is not None


_DOCKER_AVAILABLE = _docker_socket_present()
_SKIP_REASON = (
    "Docker socket unavailable in this sandbox; runs in CI where containers are provisioned."
)


@pytest.mark.skipif(not _DOCKER_AVAILABLE, reason=_SKIP_REASON)
class TestTenantFeedIntegration:
    """End-to-end smoke: publish two events → handler reads them in order.

    Drives the same publisher T3 uses (``publish_event``) so the
    integration covers the full XADD → XREVRANGE seam this resource
    wires up. Mirrors the ``TestBroadcastIntegration`` pattern from
    ``test_broadcast_client`` / ``test_broadcast_publisher``.
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

    async def test_xadd_then_resource_read_round_trip(self, valkey_url: str) -> None:
        """Publish two events; the handler reads them back oldest-first."""
        op = build_operator(TenantRole.OPERATOR)
        e_old = _make_event(op_id="vsphere.vm.list")
        e_new = _make_event(op_id="vsphere.vm.create")
        # Publish in order: old first, then new. XREVRANGE returns
        # newest-first; the handler reverses to chronological.
        await publish_event(e_old)
        await publish_event(e_new)

        result = await _tenant_feed_handler(op, {"tenant_id": str(op.tenant_id)})

        assert result["tenant_id"] == str(op.tenant_id)
        assert result["count"] == 2
        op_ids = [e["op_id"] for e in result["events"]]
        assert op_ids == ["vsphere.vm.list", "vsphere.vm.create"]
