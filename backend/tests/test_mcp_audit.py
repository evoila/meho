# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the MCP-specific audit integration (G0.5-T5, #250).

Covers every acceptance criterion on issue #250:

* ``tools/call meho.status`` writes exactly one ``audit_log`` row with
  method=MCP, path=/mcp/tools/call/meho.status, status_code=200,
  payload carries the empty-args params_hash + op_class=read.
* ``resources/read meho://tenant/<id>/info`` writes exactly one
  ``audit_log`` row with path=/mcp/resources/read/<uri>.
* ``tools/call`` against an unknown tool writes a row with status_code=404
  and op_class=unknown.
* ``compute_params_hash`` is deterministic across dict insertion order
  and equivalent representations.
* The chassis :class:`~meho_backplane.audit.AuditMiddleware` skips
  ``/mcp`` requests entirely (no duplicate row per JSON-RPC envelope).
* Audit-write failure converts an otherwise-successful MCP call into a
  JSON-RPC ``-32603`` Internal Error (fail-closed contract).
* Every row carries the correct ``tenant_id`` populated from the
  validated :class:`Operator`.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.audit import compute_params_hash, write_mcp_audit_row
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.schemas import INTERNAL_ERROR, INVALID_PARAMS, INVALID_REQUEST
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.operations.ingest.boot_stamp import BOOT_STAMP_OPERATOR_SUB
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
    seeded_operator_tenant,  # noqa: F401 — pytest-discovered fixture
)


async def _audit_rows() -> list[AuditLog]:
    """Read every MCP-request ``audit_log`` row, ordered by ``occurred_at``.

    Excludes the ``system:boot-profile-stamp`` row that
    :func:`~meho_backplane.operations.ingest.boot_stamp.stamp_catalog_profiled_connectors`
    writes at lifespan startup (#2288): the ``client_with_operator`` fixture
    enters ``TestClient(app)`` as a context manager to run the FastAPI
    lifespan, so every app-booting test in this module baselines one such
    stamp row. It is infrastructure noise unrelated to the MCP request under
    test, so scoping it out here keeps each test's row-count assertion about
    the audit rows the request itself produced.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog)
            .where(AuditLog.operator_sub != BOOT_STAMP_OPERATOR_SUB)
            .order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# compute_params_hash — pure-function determinism
# ---------------------------------------------------------------------------


def test_params_hash_empty_dict_is_stable() -> None:
    """``compute_params_hash({})`` returns the canonical empty-object hash.

    Regression-locks the hash so a future change to the canonicalisation
    rule (separators, sort_keys) would surface here loudly instead of
    silently invalidating every existing payload row's params_hash.
    """
    expected = (
        # SHA-256("{}") — the canonical JSON encoding of an empty dict
        # under ``sort_keys=True, separators=(",", ":")``.
        "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"
    )
    assert compute_params_hash({}) == expected


def test_params_hash_is_order_independent() -> None:
    """Same dict-keys in different insertion orders → same hash."""
    a = {"foo": 1, "bar": 2}
    b = {"bar": 2, "foo": 1}
    assert compute_params_hash(a) == compute_params_hash(b)


def test_params_hash_differs_for_distinct_inputs() -> None:
    """``{x: 1}`` and ``{x: 2}`` produce different digests."""
    assert compute_params_hash({"x": 1}) != compute_params_hash({"x": 2})


# ---------------------------------------------------------------------------
# tools/call audit — happy path + status_code derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_call_meho_status_writes_one_mcp_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #1: tools/call meho.status writes one ``method="MCP"`` row.

    Post-G0.6-T-Refactor-Vault, the ``meho.status`` handler reaches
    :func:`~meho_backplane.api.v1.health.build_health_response` which
    dispatches ``vault.kv.read`` through the G0.6 dispatcher; the
    dispatcher writes its own ``method="DISPATCH"`` audit row for the
    typed-op call. That row is a separate granularity (per-operation
    inside the MCP envelope) and does **not** invalidate AC #1 — the
    invariant the test pins is the **MCP-envelope** row that the
    chassis writes, not the dispatcher's internal accounting.

    Concretely: filter the audit table on ``method="MCP"`` to assert
    the single envelope row; the dispatcher row is verified
    independently in :mod:`tests.test_operations_dispatcher`.
    """
    client, op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    row = mcp_rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/meho.status"
    assert row.status_code == 200
    assert row.payload["op_id"] == "meho.status"
    assert row.payload["op_class"] == "read"
    assert row.payload["params_hash"] == compute_params_hash({})


@pytest.mark.asyncio
async def test_tools_call_unknown_tool_writes_audit_row_with_404(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC #3: unknown.tool → row with status_code=404, op_class=unknown."""
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "no.such.tool", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_PARAMS

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.path == "/mcp/tools/call/no.such.tool"
    assert row.status_code == 404
    assert row.payload["op_class"] == "unknown"


# ---------------------------------------------------------------------------
# #1481: a post-gate McpInvalidParamsError audits as "denied" (403), not 500
# ---------------------------------------------------------------------------


async def _post_gate_rejecting_handler(
    _operator: Operator,
    _arguments: dict[str, Any],
) -> dict[str, Any]:
    """Tool handler that raises ``McpInvalidParamsError`` after all gates.

    Models the production approval-queue rejections — self-approval,
    ``approval_request_not_found``, ``approval_unauthorized`` — which
    ``_approve_handler`` re-raises as :class:`McpInvalidParamsError`
    *after* the dispatcher's name/argument/RBAC/schema checks have
    already passed (so none of them set ``status_code``).
    """
    raise McpInvalidParamsError(
        "self_approval_forbidden: requester and approver must differ",
    )


@pytest.mark.asyncio
async def test_tools_call_post_gate_invalid_params_audits_as_denied_not_500(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """#1481: a post-gate ``McpInvalidParamsError`` audits as 403 "denied".

    Regression for the bug where ``status_code`` initialised to 500 and
    a handler-raised :class:`McpInvalidParamsError` (the wire
    ``-32602``) never overwrote it, so the audit row recorded a fake
    500 server crash for a clean policy rejection. The fix lives at the
    dispatch boundary so the whole class is covered — self-approval,
    ``approval_request_not_found``, ``approval_unauthorized``, and any
    future post-gate ``McpInvalidParamsError`` — not just self-approval.
    """
    client, _op = client_with_operator

    register_mcp_tool(
        ToolDefinition(
            name="test.post_gate_reject",
            description="Raises McpInvalidParamsError after the gates (test only).",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
            required_role=TenantRole.READ_ONLY,
            op_class="write",
        ),
        _post_gate_rejecting_handler,
    )

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {"name": "test.post_gate_reject", "arguments": {}},
        },
    )
    # Wire outcome: -32602 INVALID_PARAMS (a parameter/policy rejection).
    assert response.json()["error"]["code"] == INVALID_PARAMS

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    row = mcp_rows[0]
    assert row.path == "/mcp/tools/call/test.post_gate_reject"
    # The audit projection now matches the "denied" wire outcome, not 500.
    assert row.status_code == 403


# ---------------------------------------------------------------------------
# resources/read audit — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resources_read_tenant_info_writes_one_audit_row(
    client_with_operator: tuple[TestClient, Operator],
    seeded_operator_tenant: None,
) -> None:
    """AC #2: resources/read meho://tenant/<id>/info → one row, path matches."""
    client, op = client_with_operator
    uri = f"meho://tenant/{op.tenant_id}/info"

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "resources/read",
            "params": {"uri": uri},
        },
    )
    assert response.status_code == 200
    assert "result" in response.json()

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == f"/mcp/resources/read/{uri}"
    assert row.status_code == 200
    assert row.payload["uri"] == uri
    assert row.payload["op_class"] == "read"


# ---------------------------------------------------------------------------
# AC #7: chassis AuditMiddleware does NOT double-audit /mcp requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_envelope_does_not_produce_chassis_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """The chassis ``AuditMiddleware`` skips ``/mcp`` so the JSON-RPC POST
    contributes zero rows; the MCP handler writes the single envelope row.

    Without the path-prefix exclusion in :class:`AuditMiddleware`, the
    middleware would write a row per JSON-RPC POST (wrong granularity)
    AND each MCP handler would write its own row (correct granularity).
    Post-G0.6-T-Refactor-Vault, the ``meho.status`` handler also routes
    through the G0.6 dispatcher (which writes a per-operation
    ``method="DISPATCH"`` row); the contract this test pins is that
    no *chassis-middleware* row appears for the JSON-RPC POST itself —
    so we filter on ``method="MCP"`` to isolate the envelope-level row.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    # Exactly one ``method="MCP"`` row from the MCP handler; the chassis
    # AuditMiddleware path-prefix exclusion
    # (audit.py:_AUDIT_SKIP_PATH_PREFIXES) keeps the JSON-RPC envelope
    # from adding an HTTP-method row. The G0.6 dispatcher additionally
    # contributes a ``method="DISPATCH"`` row for the inner
    # ``vault.kv.read`` call; that row is intentional and verified
    # separately in :mod:`tests.test_operations_dispatcher`.
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].method == "MCP"
    http_rows = [r for r in rows if r.method in ("GET", "POST", "PUT", "DELETE", "PATCH")]
    assert http_rows == [], "chassis AuditMiddleware should skip /mcp paths; found HTTP-method rows"


# ---------------------------------------------------------------------------
# AC #5: audit-write failure → fail-closed -32603
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_write_failure_converts_call_to_internal_error(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """When :func:`write_mcp_audit_row` raises, the call fails with -32603.

    Fail-closed contract: an audit-write failure invalidates the
    operation. The client sees -32603 INTERNAL_ERROR; no
    ``result`` envelope is returned even if the handler itself
    succeeded.
    """
    client, _op = client_with_operator

    async def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("simulated audit-write failure")

    with patch(
        "meho_backplane.mcp.handlers.write_mcp_audit_row",
        side_effect=_explode,
    ):
        response = post_mcp(
            client,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "meho.status", "arguments": {}},
            },
        )

    body = response.json()
    assert body["error"]["code"] == INTERNAL_ERROR


# ---------------------------------------------------------------------------
# write_mcp_audit_row helper-level test (independent of MCP dispatch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_mcp_audit_row_persists_every_field() -> None:
    """The helper round-trips every field including the JSON ``payload``.

    The persisted payload is the caller's payload **augmented** with the
    JWT-derived ``principal_name`` (G0.15-T3 #1212); the assertion
    compares the keys the caller supplied to the persisted view of the
    same keys rather than equality on the whole dict, so the audit
    write's column-hoisting extensions stay invisible to the helper's
    caller contract while remaining verifiable.
    """
    op = build_operator()
    request_id = uuid4()
    payload = {"op_id": "test.tool", "params_hash": "abc123", "op_class": "read"}

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/test.tool",
        status_code=200,
        duration_ms=12.34,
        payload=payload,
        request_id=request_id,
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.operator_sub == op.sub
    assert row.tenant_id == op.tenant_id
    assert row.method == "MCP"
    assert row.path == "/mcp/tools/call/test.tool"
    assert row.status_code == 200
    assert row.request_id == request_id
    # SQLite stores Numeric via _PORTABLE_JSON if applicable; check value
    assert float(row.duration_ms or 0) == pytest.approx(12.34)
    # Caller-supplied keys survive the round-trip verbatim; the writer
    # additionally injects ``principal_name`` from ``Operator.name`` (the
    # fixture sets it to ``"Test"``). Assert on the caller's keys plus
    # the new G0.15-T3 #1212 columns separately so a future extension
    # of the writer (e.g. delegation-aware ``actor_name``) does not
    # silently break this test.
    persisted = dict(row.payload)
    for k, v in payload.items():
        assert persisted[k] == v
    assert persisted["principal_name"] == "Test"
    # ``Operator.email`` is None on the default fixture so no
    # ``principal_email`` key is written; explicit absence test below.
    assert "principal_email" not in persisted


# ---------------------------------------------------------------------------
# G8.2-T2 (#1010): Mcp-Session-Id capture → agent_session_id column
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_header_propagates_to_agent_session_id(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: a ``Mcp-Session-Id`` header lands on ``audit_log.agent_session_id``."""
    client, _op = client_with_operator
    session_id = uuid4()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": str(session_id)},
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id == session_id


@pytest.mark.asyncio
async def test_absent_session_header_leaves_agent_session_id_null(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: no header → ``agent_session_id`` is NULL (G0.14-T6 #1147).

    A header-less call must not invent a synthetic session id. Before
    #1147 the transport fell back to a fresh per-call ``uuid4()``,
    which produced non-NULL but completely uncorrelated rows that
    polluted the G8.2 ``audit/sessions/{id}/replay`` search surface
    (every call became its own one-row "session"). The decoupled
    capture contract leaves the column NULL when the client never
    sent a session id; the row still records the operation but is not
    part of any session walk.
    """
    client, _op = client_with_operator
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "meho.status", "arguments": {}},
    }

    assert post_mcp(client, envelope).status_code == 200
    assert post_mcp(client, {**envelope, "id": 2}).status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 2
    assert all(r.agent_session_id is None for r in mcp_rows)


@pytest.mark.asyncio
async def test_malformed_session_header_leaves_agent_session_id_null(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """AC: a non-UUID ``Mcp-Session-Id`` does not 500 — it lands NULL.

    A malformed *client* header can't go in the ``uuid`` column. The
    call still succeeds (the client is wrong; the server doesn't 500
    on a client-side mistake) but the row's ``agent_session_id``
    lands as NULL — same as a header-less call. A warning is logged
    so the misbehaving client is observable in structlog.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": "not-a-uuid"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id is None


@pytest.mark.asyncio
async def test_require_session_id_rejects_missing_header_without_audit_row(
    client_with_operator: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: ``MCP_REQUIRE_SESSION_ID=true`` + no header → -32600, no row.

    The reject must fire *before* dispatch, so no audit row is written
    for the rejected call (the operation never ran).
    """
    client, _op = client_with_operator
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
    )
    assert response.json()["error"]["code"] == INVALID_REQUEST

    rows = await _audit_rows()
    assert rows == []


@pytest.mark.asyncio
async def test_require_session_id_accepts_when_header_present(
    client_with_operator: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: require-mode still succeeds when the header is supplied."""
    client, _op = client_with_operator
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()
    session_id = uuid4()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": str(session_id)},
    )
    assert response.status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id == session_id


@pytest.mark.asyncio
async def test_require_session_id_with_malformed_header_succeeds_with_null(
    client_with_operator: tuple[TestClient, Operator],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: require-mode + malformed header → call succeeds, row is NULL.

    A present-but-malformed header satisfies the require-a-session
    contract at the transport layer (the client did send a value, just
    an unparseable one), so the dispatch is not rejected. The audit
    row's ``agent_session_id`` lands as NULL — same as the default-mode
    malformed-header path — and the structured ``mcp_malformed_session_id``
    warning lets the operator see which client is misbehaving without
    a 4xx breaking client retry logic.
    """
    client, _op = client_with_operator
    monkeypatch.setenv("MCP_REQUIRE_SESSION_ID", "true")
    get_settings.cache_clear()

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": "not-a-uuid"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id is None


@pytest.mark.asyncio
async def test_write_helper_leaves_agent_session_id_null_without_contextvar() -> None:
    """HTTP-shaped rows stay NULL: the writer reads no session contextvar.

    The chassis ``AuditMiddleware`` never binds ``mcp_session_id``, so a
    direct ``write_mcp_audit_row`` call with no contextvar bound (the
    helper-level path) leaves ``agent_session_id`` / ``parent_audit_id``
    NULL — proving the column is opt-in via the MCP transport's bind,
    not a default on every row.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    op = build_operator()

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/test.tool",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "test.tool", "op_class": "read"},
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    assert rows[0].agent_session_id is None
    assert rows[0].parent_audit_id is None


# ---------------------------------------------------------------------------
# G0.15-T3 (#1212): MCP audit-write column hoisting
#
# Covers the three findings from claude-rdc-hetzner-dc#753:
#   * Finding 1 — ``op_class`` mis-classification: ``call_operation``'s
#     outer-wrapper row now carries ``"tool_call"`` (Option A) so the inner
#     DISPATCH row remains the source of truth for the domain class.
#   * Finding 3 — ``principal_name`` / ``principal_email`` lost despite the
#     JWT carrying them: the writer now merges ``Operator.name`` /
#     ``Operator.email`` into the row payload.
#   * Finding 5 — ``target_id`` / ``target_name`` lost: the typed
#     ``target_id`` column is hoisted from the ``target_id`` contextvar the
#     targets resolver binds, and the canonical ``target_name`` lands in
#     payload (the schema has no typed name column in v0.2).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_principal_name_and_email_merged_from_operator() -> None:
    """G0.15-T3 finding 3: writer merges ``Operator.name`` / ``Operator.email``.

    Both JWT-derived claims land under ``payload['principal_name']`` /
    ``payload['principal_email']`` so a forensic query against one
    session's rows attributes activity to a human-readable identity
    without a Keycloak round-trip. ``EmailStr`` is coerced to plain
    ``str`` so the JSON encoder doesn't need to special-case the
    pydantic type.
    """
    import structlog

    from meho_backplane.auth.operator import Operator, TenantRole

    structlog.contextvars.clear_contextvars()
    op = Operator(
        sub="op-test-2",
        name="Damir Topic",
        email="damir.topic@example.com",
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
    )

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/meho.status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho.status", "op_class": "read"},
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    assert payload["principal_name"] == "Damir Topic"
    assert payload["principal_email"] == "damir.topic@example.com"
    # JSON-encoder safety: the value is a plain ``str`` instance, not an
    # ``EmailStr`` subclass; ``json.dumps(payload)`` round-trips cleanly.
    assert type(payload["principal_email"]) is str
    assert json.loads(json.dumps(payload)) == payload


@pytest.mark.asyncio
async def test_operator_without_name_or_email_writes_no_principal_keys() -> None:
    """An ``Operator`` with ``name=None`` / ``email=None`` doesn't pollute payload.

    Background: when the JWT issuer omits both ``name`` and ``email``
    claims (service-account tokens, agent principals on the CIMD path),
    the writer must not insert ``"principal_name": null`` /
    ``"principal_email": null`` placeholder entries — both because
    those waste storage on every row, and because the audit-query
    handler's surfacing logic prefers absent keys over null values to
    keep the broadcast event redaction shape stable.
    """
    import structlog

    from meho_backplane.auth.operator import Operator, TenantRole

    structlog.contextvars.clear_contextvars()
    op = Operator(
        sub="service-account-3",
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
    )

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/meho.status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho.status", "op_class": "read"},
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    payload = dict(rows[0].payload)
    assert "principal_name" not in payload
    assert "principal_email" not in payload


@pytest.mark.asyncio
async def test_explicit_payload_principal_name_wins_over_operator() -> None:
    """Caller-supplied ``principal_name`` in payload wins on collision.

    Forward-compat for a hypothetical future delegation-aware writer that
    needs to record the *acting* principal's name even when the
    :class:`Operator` slot carries the user-on-behalf identity. The
    writer uses ``setdefault``, so an explicit payload key is preserved.
    """
    import structlog

    from meho_backplane.auth.operator import Operator, TenantRole

    structlog.contextvars.clear_contextvars()
    op = Operator(
        sub="op-test-4",
        name="Damir Topic",
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=uuid4(),
        tenant_role=TenantRole.OPERATOR,
    )

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/meho.status",
        status_code=200,
        duration_ms=1.0,
        payload={
            "op_id": "meho.status",
            "op_class": "read",
            "principal_name": "Acting Agent Name",
        },
    )

    rows = await _audit_rows()
    assert rows[0].payload["principal_name"] == "Acting Agent Name"


@pytest.mark.asyncio
async def test_target_id_and_name_hoisted_from_contextvars() -> None:
    """G0.15-T3 finding 5: ``target_id`` typed column + ``target_name`` payload key.

    The :func:`~meho_backplane.targets.resolver.resolve_target` helper
    binds ``target_id`` (UUID-as-string) and ``target_name`` (string)
    to structlog contextvars at its single exit point. The MCP audit
    writer reads both — ``target_id`` lands on the typed column for
    ``query_audit target=`` lookups via the targets JOIN; ``target_name``
    lands in payload for forensic readability.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    target_id = uuid4()
    structlog.contextvars.bind_contextvars(
        target_id=str(target_id),
        target_name="rdc-vcenter",
    )
    op = build_operator()

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/call_operation",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "call_operation", "op_class": "tool_call"},
    )

    rows = await _audit_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.target_id == target_id
    assert row.payload["target_name"] == "rdc-vcenter"
    structlog.contextvars.clear_contextvars()


@pytest.mark.asyncio
async def test_target_id_malformed_contextvar_logs_and_falls_back_to_null() -> None:
    """Malformed ``target_id`` contextvar lands NULL and is logged.

    Matches the invariant-violation pattern used by the chassis
    ``_resolve_target_id`` (in ``meho_backplane.audit``): bound values
    that fail UUID parse log at error level but never block the audit
    insert — losing the row would compound a programming bug into a
    fully unaudited operation.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(target_id="not-a-uuid")
    op = build_operator()

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/call_operation",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "call_operation", "op_class": "tool_call"},
    )

    rows = await _audit_rows()
    assert rows[0].target_id is None
    structlog.contextvars.clear_contextvars()


@pytest.mark.asyncio
async def test_no_target_contextvar_leaves_target_id_null_and_no_name() -> None:
    """A tool that doesn't resolve a target leaves ``target_id`` NULL.

    ``meho.status`` and similar tenant-wide MCP tools never invoke the
    targets resolver, so the contextvar is unbound — the typed column
    must stay NULL (the chassis-era default) and no ``target_name`` key
    should be injected into payload.
    """
    import structlog

    structlog.contextvars.clear_contextvars()
    op = build_operator()

    await write_mcp_audit_row(
        operator=op,
        method="MCP",
        path="/mcp/tools/call/meho.status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho.status", "op_class": "read"},
    )

    rows = await _audit_rows()
    assert rows[0].target_id is None
    assert "target_name" not in rows[0].payload


@pytest.mark.asyncio
async def test_call_operation_tool_definition_carries_tool_call_op_class() -> None:
    """G0.15-T3 finding 1: ``call_operation``'s ToolDefinition uses ``op_class="tool_call"``.

    The outer-wrapper row's ``op_class`` comes from the tool def via
    ``handle_tools_call`` (``mcp/handlers.py``), so pinning the def's
    value is equivalent to pinning every MCP envelope row for that
    tool. The inner DISPATCH row writes a separate row through
    ``operations._audit.write_audit_row`` whose ``op_class`` is the
    classifier output for the *inner* op_id — unchanged by this fix
    and verified separately in ``test_operations_dispatcher``.
    """
    from meho_backplane.mcp.registry import get_tool

    entry = get_tool("call_operation")
    assert entry is not None
    defn, _handler = entry
    assert defn.op_class == "tool_call"


# ---------------------------------------------------------------------------
# G0.15-T4 (#1213): end-to-end roundtrip — server issues Mcp-Session-Id on
# initialize, client echoes it on tools/call, audit row lands the UUID.
#
# The positive-path acceptance test for the issuance+capture+write chain
# the v0.7.0 release-body's G0.14-T6 #1147 callout promised. The unit
# tests in :mod:`tests.test_mcp_server` cover the issuance half in
# isolation; this test exercises the full end-to-end contract any
# spec-conforming MCP client would drive (server assigns id on
# initialize → client echoes on every later POST → row carries it).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_issued_session_id_round_trips_to_audit_row(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """End-to-end: server-issued session id propagates onto ``audit_log.agent_session_id``.

    The chain under test:

    1. Client POSTs ``initialize`` to ``/mcp`` (no inbound session header).
    2. Server stamps an ``Mcp-Session-Id`` response header per MCP
       2025-06-18 §"Session Management" rule 1.
    3. Client echoes that id on a subsequent ``tools/call`` per rule 2.
    4. Server's :func:`~meho_backplane.mcp.server._bind_mcp_session_id`
       captures the inbound header into the structlog contextvar.
    5. :func:`~meho_backplane.mcp.audit.write_mcp_audit_row` reads the
       contextvar and writes ``audit_log.agent_session_id``.

    Before G0.15-T4 #1213, step 2 didn't happen — the client had no
    server-assigned id to echo back, so steps 4 and 5 saw an empty
    inbound header and the column landed NULL despite ``meho_status``
    advertising ``mcp_session_id_capture: "always"``. This test
    regression-locks the full chain so the next consumer-side dogfood
    cycle doesn't relive `claude-rdc-hetzner-dc#753` finding 2.
    """
    from uuid import UUID

    client, _op = client_with_operator

    # 1+2. Initialize handshake — server issues the session id.
    init = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        },
    )
    assert init.status_code == 200
    issued_header = init.headers.get("mcp-session-id")
    assert issued_header, (
        "initialize must issue an Mcp-Session-Id response header per MCP "
        "2025-06-18 §Session Management rule 1 — G0.15-T4 #1213 contract"
    )
    session_uuid = UUID(issued_header)

    # 3+4+5. Subsequent tools/call echoes the issued id — captured into
    # the contextvar by ``_bind_mcp_session_id``, written onto the row
    # by ``write_mcp_audit_row``.
    call = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "meho.status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": issued_header},
    )
    assert call.status_code == 200

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].agent_session_id == session_uuid


# ---------------------------------------------------------------------------
# #1481: broadcast classification of the corrected post-gate status
# ---------------------------------------------------------------------------


def test_classify_mcp_status_403_is_denied_not_error() -> None:
    """#1481: the 403 a post-gate rejection now records classifies "denied".

    Locks the broadcast half of the fix — once the audit ``status_code``
    is corrected from the init 500 to 403, ``_classify_mcp_status`` maps
    it to ``"denied"`` (not ``"error"``), so the live feed event
    reflects a policy rejection rather than a fake server crash. 500
    stays ``"error"`` so a genuine handler fault is still surfaced.
    """
    from meho_backplane.mcp.handlers import _classify_mcp_status

    assert _classify_mcp_status(403) == "denied"
    assert _classify_mcp_status(500) == "error"
    assert _classify_mcp_status(200) == "ok"
