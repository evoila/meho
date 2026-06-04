# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Regression tests for #93: ``call_operation`` envelope broadcast clamping.

Bug: ``handle_tools_call`` called ``compute_effective_broadcast_detail``
with ``op_id=audit_name`` (the literal string ``"call_operation"``).
``classify_op("call_operation")`` falls through to ``"other"`` →
``_default_detail("other")`` returns ``"full"`` → the broadcast event's
``payload`` contained ``params`` with the raw agent arguments, including
secret-bearing fields (``vault.kv.put``'s ``params.data``,
``vault.auth.userpass.write``'s ``params.password``).

Fix (#93): when ``audit_name == "call_operation"`` and ``arguments``
carries a non-empty string ``op_id``, the resolver is called with the
inner op_id so ``classify_op`` sees the real operation and clamps
credential classes to aggregate-only — matching the inner DISPATCH row's
precedent at ``operations/_audit.py:443``.

Coverage matrix (per issue #93 acceptance criteria):

* AC#1 / AC#2: every op_id in the credential-bearing classes
  (``credential_write``, ``credential_mint``, ``credential_read``) dispatched
  via ``call_operation`` produces an envelope ``BroadcastEvent`` whose
  ``payload`` is ``{op_class, result_status}`` with **no** ``params`` key
  and no secret string anywhere in the serialised payload.
* AC#3: a non-secret inner op (``k8s.node.list`` → ``read``) still
  broadcasts at full detail — the envelope ``payload`` contains a ``params``
  key so the fix does not over-clamp non-secret ops.
* AC#5 / AC#6: the existing MCP audit-row tests for ``call_operation`` pass
  unmodified; the envelope's ``op_id`` and audit ``path`` are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.broadcast import BroadcastEvent
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# ---------------------------------------------------------------------------
# Canary secret value — asserted to NEVER appear in broadcast payloads
# ---------------------------------------------------------------------------

_SECRET_TOKEN = "s3cr3t-canary-must-not-leak-onto-feed"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools_call_envelope(
    inner_op_id: str,
    params: dict[str, Any] | None = None,
    call_id: int = 1,
) -> dict[str, Any]:
    """Build a ``tools/call call_operation`` JSON-RPC request body."""
    arguments: dict[str, Any] = {
        "connector_id": "vault-1.x",
        "op_id": inner_op_id,
    }
    if params is not None:
        arguments["params"] = params
    return {
        "jsonrpc": "2.0",
        "id": call_id,
        "method": "tools/call",
        "params": {"name": "call_operation", "arguments": arguments},
    }


def _dummy_dispatch_result(op_id: str) -> dict[str, Any]:
    """Minimal ``call_operation`` handler return value."""
    return {"status": "ok", "op_id": op_id, "duration_ms": 1.0, "result": {}}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_broadcast(monkeypatch: pytest.MonkeyPatch) -> list[BroadcastEvent]:
    """Intercept ``publish_event`` in the MCP handlers module.

    ``publish_event`` is imported into ``meho_backplane.mcp.handlers``
    at module load time, so the patch target is the name as bound in
    that module's namespace, not the canonical module where the symbol
    lives.  This mirrors the pattern in
    :mod:`tests.test_connectors_vcf_automation_credread`.
    """
    import meho_backplane.mcp.handlers as handlers_module

    events: list[BroadcastEvent] = []

    async def _capture(event: BroadcastEvent) -> None:
        events.append(event)

    monkeypatch.setattr(handlers_module, "publish_event", _capture)
    return events


@pytest.fixture
def stub_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out ``write_mcp_audit_row`` so tests don't need a real DB write.

    The audit contract (row shape, fail-closed semantics) is covered by
    :mod:`tests.test_mcp_audit`; these tests focus exclusively on the
    broadcast-payload content.
    """
    import meho_backplane.mcp.handlers as handlers_module

    async def _noop(**_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(handlers_module, "write_mcp_audit_row", _noop)


@pytest.fixture
def stub_call_operation() -> Iterator[AsyncMock]:
    """Stub the ``call_operation`` meta-tool function to avoid real dispatch.

    Patches the name as bound in ``meho_backplane.mcp.tools.operations``
    (the module that registers the ``call_operation`` MCP tool and calls
    this function from ``_call_operation_handler``).  Returns a mock that
    echoes the ``op_id`` from the arguments so callers can introspect it.
    """
    with patch(
        "meho_backplane.mcp.tools.operations.call_operation",
        new_callable=AsyncMock,
    ) as mock_fn:

        async def _echo_result(_operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
            return _dummy_dispatch_result(arguments.get("op_id", "unknown"))

        mock_fn.side_effect = _echo_result
        yield mock_fn


# ---------------------------------------------------------------------------
# AC#1 + AC#2: credential-bearing ops → aggregate-only envelope payload
# ---------------------------------------------------------------------------

_CREDENTIAL_WRITE_OPS = [
    # AC#1 canonical example (issue body)
    ("vault.kv.put", {"path": "secret/app", "data": {"token": _SECRET_TOKEN}}),
    # AC#2 parametrized coverage
    ("vault.kv.patch", {"path": "secret/app", "data": {"token": _SECRET_TOKEN}}),
    ("vault.auth.userpass.write", {"username": "svc", "password": _SECRET_TOKEN}),
    (
        "vault.auth.userpass.update_password",
        {"username": "svc", "password": _SECRET_TOKEN},
    ),
]

_CREDENTIAL_MINT_OPS = [
    ("vault.token.create", {"policies": ["default"]}),
    ("vault.auth.approle.generate_secret_id", {"role_name": "my-role"}),
    ("harbor.robot.create", {"name": "ci-bot", "permissions": []}),
]

_CREDENTIAL_READ_OPS = [
    ("vault.kv.read", {"path": "secret/app"}),
    ("vault.kv.list", {"path": "secret/"}),
]

_ALL_CREDENTIAL_OPS = _CREDENTIAL_WRITE_OPS + _CREDENTIAL_MINT_OPS + _CREDENTIAL_READ_OPS


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.parametrize("inner_op_id,op_params", _ALL_CREDENTIAL_OPS)
@pytest.mark.asyncio
async def test_call_operation_credential_inner_op_broadcasts_aggregate_only(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    captured_broadcast: list[BroadcastEvent],
    stub_audit: None,
    stub_call_operation: AsyncMock,
    inner_op_id: str,
    op_params: dict[str, Any],
) -> None:
    """AC#1 + AC#2: a credential-class inner op via ``call_operation`` never
    ships ``params`` on the broadcast feed.

    Asserts both the structural absence of ``"params"`` in the envelope
    ``BroadcastEvent.payload`` AND the full absence of the canary secret
    string from the serialised payload, so a future shape change that
    embeds the params under a different key doesn't quietly regress.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        _tools_call_envelope(inner_op_id=inner_op_id, params=op_params),
    )
    # The stub handler succeeds; the MCP envelope returns isError=False.
    body = response.json()
    assert "error" not in body, f"unexpected MCP error for {inner_op_id}: {body}"
    assert body["result"]["isError"] is False

    # Exactly one broadcast event should have been published.
    assert len(captured_broadcast) == 1, (
        f"expected 1 broadcast event for {inner_op_id}, got {len(captured_broadcast)}"
    )
    event = captured_broadcast[0]

    # The payload must be the aggregate shape: {op_class, result_status}.
    assert "params" not in event.payload, (
        f"credential inner op {inner_op_id!r} leaked params onto the feed: {event.payload}"
    )

    # Belt-and-suspenders: the canary secret must not appear anywhere in
    # the serialised event, even if the shape changes under a new key.
    serialised = json.dumps(event.payload)
    assert _SECRET_TOKEN not in serialised, (
        f"canary secret found in broadcast payload for {inner_op_id!r}: {serialised}"
    )

    # The result_status field must be present (aggregate-only contract).
    assert "result_status" in event.payload
    assert "op_class" in event.payload


# ---------------------------------------------------------------------------
# AC#3: non-secret inner op → full-detail envelope payload
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_call_operation_non_secret_inner_op_broadcasts_full_detail(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    captured_broadcast: list[BroadcastEvent],
    stub_audit: None,
    stub_call_operation: AsyncMock,
) -> None:
    """AC#3: a non-secret inner op (``k8s.node.list``) keeps full-detail broadcast.

    The fix must not over-clamp: operators still see request params on the
    feed for routine, non-credential operations so the team-coordination
    signal remains useful.
    """
    client, _op = client_with_operator
    inner_op_id = "k8s.node.list"
    op_params = {"namespace": "kube-system"}

    response = post_mcp(
        client,
        _tools_call_envelope(inner_op_id=inner_op_id, params=op_params),
    )
    body = response.json()
    assert "error" not in body, f"unexpected MCP error for {inner_op_id}: {body}"
    assert body["result"]["isError"] is False

    assert len(captured_broadcast) == 1
    event = captured_broadcast[0]

    # Full-detail shape: payload must contain a ``params`` key with the
    # request arguments visible to the feed subscriber.
    assert "params" in event.payload, (
        f"non-secret inner op {inner_op_id!r} was over-clamped — "
        f"'params' missing from broadcast payload: {event.payload}"
    )


# ---------------------------------------------------------------------------
# AC#5: audit op_id + path are unchanged (envelope uses wrapper tool name)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
@pytest.mark.asyncio
async def test_call_operation_broadcast_event_op_id_is_wrapper_name(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
    captured_broadcast: list[BroadcastEvent],
    stub_audit: None,
    stub_call_operation: AsyncMock,
) -> None:
    """AC#5: the envelope ``BroadcastEvent.op_id`` stays ``"call_operation"``.

    Only the broadcast *detail* (payload shape) changes; the event's
    identifying ``op_id`` field must remain the wrapper tool name so
    ``meho audit query`` cardinality and path-based correlations are
    unchanged.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        _tools_call_envelope(
            inner_op_id="vault.kv.put",
            params={"path": "secret/x", "data": {"password": _SECRET_TOKEN}},
        ),
    )
    assert "error" not in response.json()

    assert len(captured_broadcast) == 1
    event = captured_broadcast[0]

    # The event's op_id is the MCP tool name, not the inner op.
    assert event.op_id == "call_operation"
