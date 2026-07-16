# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for ``meho.broadcast.announce`` (G6.4-T2, #1092).

Acceptance-criteria coverage (per issue body):

* ``meho.broadcast.announce`` is registered and visible on ``tools/list``
  for an ``operator`` JWT; hidden from ``read_only``.
* ``activity`` length is capped at 500 chars; over-length input rejects
  with JSON-RPC ``-32602`` Invalid Params.
* Cross-tenant isolation: tenant-A's announcement is NOT visible to
  tenant-B's ``meho.broadcast.recent``; the stream key is derived
  exclusively from ``operator.tenant_id``.
* Valkey unreachable during publish → handler raises (NOT fail-open);
  error propagates as ``-32603`` Internal Error (distinct from the
  audit-driven publisher's silent swallow).
* The ``broadcast_agent_announcements_total{phase}`` Prometheus counter
  increments per successful publish, labelled by the call's ``phase``.
* Round-trip: an announce → recent pull surfaces the new event with the
  ``event_kind="agent_announcement"`` discriminator and the
  agent-authored fields intact.

The mocked-client suite covers the wire surface (registration, schema,
validation, handler glue, metric increment, fail-loud propagation). The
Docker-gated ``TestBroadcastAnnounceIntegration`` suite spins up
``valkey/valkey:8`` via testcontainers and drives the full publish →
xrange → tool-handler seam, including the cross-tenant isolation
guarantee and the announce → recent round-trip.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import (
    ACTIVITY_MAX_CHARS,
    BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL,
    AgentAnnouncementEvent,
    dispose_broadcast_client,
    get_broadcast_client,
    publish_agent_announcement,
    reset_broadcast_client_for_testing,
)
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS
from meho_backplane.mcp.tools.broadcast import (
    _handler_announce,
    _handler_recent,
)
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import wrap_untrusted_text
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

# ---------------------------------------------------------------------------
# Per-test broadcast-client isolation (mirrors test_mcp_tool_broadcast_recent)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_broadcast_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear the cached client + pin a stub URL around every test.

    Pinning ``BROADCAST_REDIS_URL`` keeps the construction path free of
    "URL not set" failures; per-test patches replace ``xadd`` / ``xrange``
    so no socket ever opens.
    """
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    yield
    reset_broadcast_client_for_testing()
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _disable_announce_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the per-principal rate limit for the wire-surface tests.

    These tests mock ``xadd`` on the fast client and never open a socket;
    the rate limiter (G6.5-T6 #2546) would otherwise issue a real
    ``INCR``/``EXPIRE`` against the stub URL and fail. Setting the knob to
    ``0`` exercises the limiter's real "unlimited" early-return (no Valkey
    round-trip), so these tests stay socket-free. The limiter's own
    behaviour is covered in ``test_broadcast_announce_rate_limit.py`` (unit)
    and this file's Docker-gated integration suite exercises the real
    ``_handler_announce`` under a configured limit.

    Runs after ``_isolated_broadcast_client`` (which clears the settings
    cache) via the explicit dependency below, so the ``0`` is what the
    next ``get_settings()`` reads.

    The env knob documents intent, but the load-bearing guard is the
    ``enforce_announce_rate_limit`` patch: the env->``get_settings``
    route alone is fragile under the app fixture, which can repopulate
    the ``lru_cache`` with the default limit after the cache clear
    (passed single-process locally, tripped a real socket ->
    ``-32603 ConnectionError`` under xdist ``loadscope`` in CI on the
    sibling ``test_broadcast_structured_claims.py``). Patching the
    function bypasses settings caching entirely.
    """
    monkeypatch.setenv("BROADCAST_ANNOUNCE_RATE_PER_MINUTE", "0")
    get_settings.cache_clear()
    monkeypatch.setattr(
        "meho_backplane.mcp.tools.broadcast.enforce_announce_rate_limit",
        AsyncMock(return_value=None),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
    content = body["result"]["content"]
    return json.loads(content[0]["text"])


def _counter_value(counter: Any, **labels: str) -> float:
    """Read a Prometheus counter's current value.

    ``Counter._value`` is private but stable across prometheus-client
    versions; the public surface only emits exposition text. The metric
    is module-scoped so the value persists across tests in the same
    process -- every test that asserts on a counter captures the
    baseline before the call and computes the delta.
    """
    if labels:
        child = counter.labels(**labels)
        return float(child._value.get())  # type: ignore[no-any-return]
    return float(counter._value.get())  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Registration shape + role visibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_tools_list_exposes_announce_for_operator(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``operator`` role sees ``meho.broadcast.announce`` on tools/list."""
    client, _op = client_with_operator
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.announce" in names

    announce_def = next(
        t for t in body["result"]["tools"] if t["name"] == "meho.broadcast.announce"
    )
    # MEHO-internal RBAC fields stripped from the wire shape.
    assert "required_role" not in announce_def
    assert "op_class" not in announce_def

    schema = announce_def["inputSchema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["activity"]
    # Length cap surfaces on the wire so client-side JSON-Schema
    # validators can short-circuit before the round-trip.
    assert schema["properties"]["activity"]["maxLength"] == 500
    assert schema["properties"]["activity"]["minLength"] == 1
    # Phase enum exposed verbatim.
    assert schema["properties"]["phase"]["enum"] == ["start", "update", "completion"]
    assert schema["properties"]["phase"]["default"] == "update"


def test_tools_list_hides_announce_from_read_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``read_only`` operator does NOT see the operator-gated tool."""
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    body = resp.json()
    names = {t["name"] for t in body["result"]["tools"]}
    assert "meho.broadcast.announce" not in names


def test_read_only_tools_call_announce_is_rejected(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A direct ``tools/call`` from below ``operator`` rejects with -32602.

    The registry filter hides the tool from ``tools/list``; the
    dispatcher's call-time RBAC re-check is the load-bearing second
    gate against a client that knows the name and posts anyway.
    """
    client, _op = client_with_operator  # default fixture role is READ_ONLY
    resp = post_mcp(
        client,
        _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
    )
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS
    assert "forbidden" in body["error"]["message"].lower()


# ---------------------------------------------------------------------------
# Activity length cap (acceptance: overflow → -32602)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_activity_at_exactly_cap_is_accepted(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``activity`` of exactly 500 chars publishes cleanly."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")):
        resp = post_mcp(
            client,
            _tools_call(
                "meho.broadcast.announce",
                {"activity": "x" * ACTIVITY_MAX_CHARS},
            ),
        )
    body = resp.json()
    assert "error" not in body, body


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_activity_one_over_cap_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``activity`` of 501 chars rejects with JSON-RPC -32602.

    Two layers enforce the cap: the JSON-Schema ``maxLength: 500`` at
    the dispatcher validates first; the handler's explicit re-check
    catches anything a future schema-bypass admits. Either way the
    surface is INVALID_PARAMS.
    """
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho.broadcast.announce",
            {"activity": "x" * (ACTIVITY_MAX_CHARS + 1)},
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
def test_activity_empty_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``activity`` empty string rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho.broadcast.announce", {"activity": ""}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_activity_missing_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``activity`` absent rejects (required by schema)."""
    client, _op = client_with_operator
    resp = post_mcp(client, _tools_call("meho.broadcast.announce", {}))
    body = resp.json()
    assert "error" in body
    assert body["error"]["code"] == INVALID_PARAMS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_phase_outside_enum_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``phase`` outside the start/update/completion enum rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho.broadcast.announce",
            {"activity": "x", "phase": "in-progress"},
        ),
    )
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
    """The handler ALWAYS writes to ``meho:feed:{operator.tenant_id}``.

    Tenant isolation here is structural: the input schema has no
    ``tenant_id`` field, so a malicious caller has no way to write to
    another tenant's stream. Asserting the xadd key is the right shape
    proves the structural property holds.

    The audit-driven publish-on-write hook
    (:func:`meho_backplane.mcp.handlers._publish_after_dispatch` /
    :func:`meho_backplane.broadcast.publisher.publish_event`) ALSO
    calls ``xadd`` for the ``meho.broadcast.announce`` invocation
    itself -- the MCP call is an audited operation, so two xadd
    calls land per request: one for the announcement content (this
    handler), one for the audit-driven sibling event. Both target
    the same per-tenant key, but the assertion below pins the
    announcement-side call distinctly by matching against the
    JSON wire shape (``event_kind = "agent_announcement"``).
    """
    client, op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")) as xa:
        post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )
    expected_key = f"meho:feed:{op.tenant_id}"
    # Every xadd MUST target the operator's tenant -- structural
    # tenancy holds for the announcement AND the audit-driven sibling.
    keys_written = {call.args[0] for call in xa.await_args_list}
    assert keys_written == {expected_key}
    # And at least one of those calls carried the agent-announcement
    # payload (proves the structural-tenant guarantee specifically on
    # the new write path, not just on the audit sibling).
    announce_payloads = [
        call.args[1]
        for call in xa.await_args_list
        if "agent_announcement" in call.args[1].get("event", "")
    ]
    assert len(announce_payloads) == 1, "expected exactly one announcement xadd"


async def test_handler_with_distinct_operator_writes_distinct_stream() -> None:
    """Two operators on different tenants write to their own streams only.

    Directly exercises the handler (bypassing the FastAPI dispatcher)
    with two operators bound to two different tenant ids; asserts each
    call resolves to its own stream key. The mocked client always
    returns a fake entry id -- the assertion is on the key passed to
    xadd, not on what came back.
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
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")) as xa:
        await _handler_announce(op_a, {"activity": "alpha"})
        await _handler_announce(op_b, {"activity": "beta"})

    keys_written = [call.args[0] for call in xa.await_args_list]
    assert keys_written == [f"meho:feed:{tenant_a}", f"meho:feed:{tenant_b}"]


# ---------------------------------------------------------------------------
# Fail-loud publish (Valkey unreachable propagates)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_valkey_unreachable_surfaces_as_internal_error(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A redis-py ConnectionError on xadd surfaces as JSON-RPC -32603.

    The fail-loud contract is the load-bearing difference from
    :func:`publish_event` (audit-driven, fail-open). The agent
    explicitly emitted the announcement and must know whether it
    landed -- a swallowed error would leave the agent thinking it
    told the team while the team never saw it.
    """
    from redis import exceptions as redis_exceptions

    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(
        bc,
        "xadd",
        new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
    ):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )
    body = resp.json()
    assert "error" in body
    # JSON-RPC -32603 Internal Error -- the dispatcher's generic
    # exception handler maps non-McpInvalidParamsError exceptions here.
    assert body["error"]["code"] == INTERNAL_ERROR


async def test_publish_agent_announcement_propagates_exception() -> None:
    """``publish_agent_announcement`` directly raises on Valkey errors.

    Unit-level companion to the dispatcher test above -- asserts the
    publisher entry point itself is fail-loud, independent of MCP
    plumbing. This is the contract the announce handler depends on;
    the audit-driven :func:`publish_event` has the opposite contract
    (silent swallow) and the test guards against an accidental
    refactor that would unify them.
    """
    from datetime import UTC, datetime

    from redis import exceptions as redis_exceptions

    event = AgentAnnouncementEvent(
        tenant_id=OPERATOR_TENANT_ID,
        principal_sub="op-test",
        activity="investigating",
        phase="start",
        ts=datetime.now(UTC),
    )
    bc = get_broadcast_client()
    with (
        patch.object(
            bc,
            "xadd",
            new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
        ),
        pytest.raises(redis_exceptions.ConnectionError),
    ):
        await publish_agent_announcement(event)


# ---------------------------------------------------------------------------
# Successful publish: shape + metric increment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_successful_publish_returns_event_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """On success the handler returns the entry id as ``cursor`` + ``event_id``.

    Both keys carry the Valkey stream entry id of the appended
    announcement: ``cursor`` is the self-labelled canonical name
    (round-trips through recent/watch's ``cursor`` arg, #2479);
    ``event_id`` is the legacy alias kept for wire compatibility --
    it is NOT a durable event UUID.
    """
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-7")):
        resp = post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )
    result = _result_dict(resp)
    assert result == {"event_id": "1747800000000-7", "cursor": "1747800000000-7"}


@pytest.mark.parametrize(
    "client_with_operator,phase_label",
    [
        (TenantRole.OPERATOR, "start"),
        (TenantRole.OPERATOR, "update"),
        (TenantRole.OPERATOR, "completion"),
    ],
    indirect=["client_with_operator"],
)
def test_metric_increments_per_phase(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    phase_label: str,
) -> None:
    """``broadcast_agent_announcements_total{phase}`` increments per publish.

    Counter is module-scoped (persists across the process); each test
    captures the per-label baseline before the call and asserts the
    delta is exactly 1.
    """
    client, _op = client_with_operator
    bc = get_broadcast_client()
    baseline = _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase=phase_label)
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")):
        post_mcp(
            client,
            _tools_call(
                "meho.broadcast.announce",
                {"activity": "x", "phase": phase_label},
            ),
        )
    after = _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase=phase_label)
    assert after - baseline == 1


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_phase_defaults_to_update(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """When ``phase`` is omitted the publish credits the ``update`` counter."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    baseline = _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase="update")
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")):
        post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )
    assert _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase="update") - baseline == 1


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_failed_publish_does_not_increment_metric(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """A swallowed publish must NOT count as a successful announcement."""
    from redis import exceptions as redis_exceptions

    client, _op = client_with_operator
    bc = get_broadcast_client()
    baseline = _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase="start")
    with patch.object(
        bc,
        "xadd",
        new=AsyncMock(side_effect=redis_exceptions.ConnectionError("refused")),
    ):
        post_mcp(
            client,
            _tools_call(
                "meho.broadcast.announce",
                {"activity": "x", "phase": "start"},
            ),
        )
    # Counter MUST stay flat -- a failed publish is not a success.
    assert _counter_value(BROADCAST_AGENT_ANNOUNCEMENTS_TOTAL, phase="start") == baseline


# ---------------------------------------------------------------------------
# Wire-shape of the XADD'd JSON (event_kind discriminator)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_published_event_carries_agent_announcement_kind(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """The JSON written to the stream carries the discriminator + fields.

    The wire shape is identical to BroadcastEvent's framing
    (``{event: <json>}`` field on the XADD call). The discriminator
    is the ``event_kind`` field inside the JSON; T1's reader uses it
    to pick the right model class.
    """
    client, op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")) as xa:
        post_mcp(
            client,
            _tools_call(
                "meho.broadcast.announce",
                {
                    "activity": "investigating cluster X latency",
                    "target": "prod-vc-1",
                    "scope": "vCenter perf",
                    "phase": "start",
                },
            ),
        )

    # Two xadd calls land per request: one from the announcement
    # handler (the AGENT-authored payload this test asserts on) and one
    # from the chassis audit-driven publish-on-write hook (the audit
    # sibling BroadcastEvent for the meho.broadcast.announce MCP call
    # itself). Pick the announcement-side payload explicitly by
    # event_kind so the assertion stays robust to the call ordering.
    decoded_payloads = [
        json.loads(call.args[1]["event"]) for call in xa.await_args_list if "event" in call.args[1]
    ]
    announce_payloads = [p for p in decoded_payloads if p.get("event_kind") == "agent_announcement"]
    assert len(announce_payloads) == 1, "expected exactly one announcement xadd"
    decoded = announce_payloads[0]
    assert decoded["tenant_id"] == str(op.tenant_id)
    assert decoded["principal_sub"] == op.sub
    assert decoded["activity"] == "investigating cluster X latency"
    assert decoded["target"] == "prod-vc-1"
    assert decoded["scope"] == "vCenter perf"
    assert decoded["phase"] == "start"
    assert "ts" in decoded
    # And -- by design -- no audit_id / op_id / op_class on the agent
    # announcement (the AGENT authored it; it's not derived from an
    # audit row).
    assert "audit_id" not in decoded
    assert "op_id" not in decoded
    assert "op_class" not in decoded


# ---------------------------------------------------------------------------
# Optional target / scope behaviour
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_target_and_scope_default_to_none(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Omitting ``target`` / ``scope`` lands ``None`` on the published event."""
    client, _op = client_with_operator
    bc = get_broadcast_client()
    with patch.object(bc, "xadd", new=AsyncMock(return_value="1747800000000-0")) as xa:
        post_mcp(
            client,
            _tools_call("meho.broadcast.announce", {"activity": "investigating"}),
        )

    # Pick the announcement-side xadd specifically -- the audit-driven
    # sibling publish also lands a BroadcastEvent and would otherwise
    # shadow the assertion target. See the dedicated
    # ``event_kind`` discriminator test for the rationale.
    announce_payloads = [
        json.loads(call.args[1]["event"])
        for call in xa.await_args_list
        if "event" in call.args[1]
        and json.loads(call.args[1]["event"]).get("event_kind") == "agent_announcement"
    ]
    assert len(announce_payloads) == 1
    decoded = announce_payloads[0]
    assert decoded["target"] is None
    assert decoded["scope"] is None


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_target_wrong_type_returns_invalid_params(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``target`` of the wrong JSON type rejects with -32602."""
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        _tools_call(
            "meho.broadcast.announce",
            {"activity": "x", "target": 42},
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
class TestBroadcastAnnounceIntegration:
    """End-to-end suite against a real Valkey container.

    Drives the same publisher (``publish_agent_announcement``) the MCP
    handler uses so the integration covers the full XADD → XRANGE →
    handler seam, including the cross-tenant isolation contract and
    the announce → recent round-trip.
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

    async def test_announce_then_recent_round_trip(self, valkey_url: str) -> None:
        """An announce lands; the same operator reads it back via recent."""
        op = build_operator(TenantRole.OPERATOR)
        result = await _handler_announce(
            op,
            {
                "activity": "investigating",
                "target": "prod-vc-1",
                "phase": "start",
            },
        )
        assert "event_id" in result
        # Announce self-labels the entry id as ``cursor`` (#2479);
        # ``event_id`` is the legacy alias of the same stream cursor.
        assert result["cursor"] == result["event_id"]

        recent = await _handler_recent(op, {})
        assert len(recent["events"]) == 1
        event = recent["events"][0]
        assert event["event_kind"] == "agent_announcement"
        assert event["cursor"] == result["cursor"]
        # dump_event_wire re-serves agent-authored free text inside the
        # untrusted-content envelope (_ANNOUNCEMENT_UNTRUSTED_FIELDS).
        assert event["activity"] == wrap_untrusted_text("investigating")
        assert event["target"] == wrap_untrusted_text("prod-vc-1")
        assert event["phase"] == "start"
        assert event["principal_sub"] == op.sub
        assert event["tenant_id"] == str(op.tenant_id)

    async def test_two_tenants_see_only_their_own_announcements(
        self,
        valkey_url: str,
    ) -> None:
        """Cross-tenant isolation: tenant-A's announce isn't visible to tenant-B.

        The structural tenant guarantee verified end-to-end: tenant-A's
        operator never sees tenant-B's announcement even when both
        streams sit on the same Valkey instance.
        """
        tenant_a = UUID("aaaa0000-0000-0000-0000-000000000010")
        tenant_b = UUID("bbbb0000-0000-0000-0000-000000000020")
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

        await _handler_announce(op_a, {"activity": "tenant-a-secret"})
        await _handler_announce(op_b, {"activity": "tenant-b-secret"})

        recent_a = await _handler_recent(op_a, {})
        recent_b = await _handler_recent(op_b, {})

        a_activities = [e["activity"] for e in recent_a["events"]]
        b_activities = [e["activity"] for e in recent_b["events"]]
        assert a_activities == [wrap_untrusted_text("tenant-a-secret")]
        assert b_activities == [wrap_untrusted_text("tenant-b-secret")]
        # Belt-and-suspenders cross-leak negative assertion, wrap-aware:
        # the wire values are enveloped, so probe for the enveloped form
        # (the raw string would trivially pass against wrapped entries).
        assert wrap_untrusted_text("tenant-b-secret") not in a_activities
        assert wrap_untrusted_text("tenant-a-secret") not in b_activities
