# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the MCP Streamable HTTP transport entrypoint (G0.5-T1, #246).

Covers the acceptance criteria on issue #246:

* ``/mcp`` accepts JSON-RPC 2.0 POST bodies.
* ``initialize`` returns ``protocolVersion=2025-06-18``, capabilities,
  and a ``serverInfo`` payload pinned to the running app's version.
* ``initialize`` with missing ``protocolVersion`` returns
  :data:`~meho_backplane.mcp.schemas.INVALID_PARAMS` (``-32602``).
* ``notifications/initialized`` is acknowledged with HTTP 202 and no
  body, per MCP Streamable HTTP §"Sending Messages to the Server".
* Unknown methods return :data:`~meho_backplane.mcp.schemas.METHOD_NOT_FOUND`
  (``-32601``) for requests, and 202 (silently dropped) for notifications.
* Parse errors return :data:`~meho_backplane.mcp.schemas.PARSE_ERROR`
  (``-32700``) with ``id: null`` per JSON-RPC spec §5.
* Wrong ``jsonrpc`` envelope returns
  :data:`~meho_backplane.mcp.schemas.INVALID_REQUEST` (``-32600``).
* ``ping`` returns ``result: {}``.
* Bearer-auth-free calls succeed (T1 has no auth; T2 adds it).

Note on AC #3 wording
=====================

The issue body's AC list says ``notifications/initialized`` "returns 204
no content". The MCP Streamable HTTP spec requires HTTP **202** Accepted
for notifications (§"Sending Messages to the Server", "the server MUST
return HTTP status code 202 Accepted with no body"). The spec MUST
overrides the AC's HTTP-code detail — the intent ("acknowledge with no
body") is met either way; the wire code follows the spec.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane import __version__
from meho_backplane.main import app
from meho_backplane.mcp.schemas import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
)


@pytest.fixture
def client() -> TestClient:
    """Plain :class:`TestClient` for the running app.

    The shared :mod:`conftest` autouse fixture supplies a per-test
    SQLite ``DATABASE_URL`` with the schema migrated, so the app's
    lifespan and middleware chain boot cleanly under
    :class:`fastapi.testclient.TestClient` without any per-test
    plumbing here.
    """
    return TestClient(app)


def _post_mcp(client: TestClient, body: Any) -> Any:
    """POST *body* (JSON) to ``/mcp`` and return the response object."""
    return client.post("/mcp", json=body)


# ---------------------------------------------------------------------------
# initialize handshake
# ---------------------------------------------------------------------------


def test_initialize_returns_protocol_version_capabilities_and_serverinfo(
    client: TestClient,
) -> None:
    """A well-formed ``initialize`` returns the spec-mandated payload."""
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.1"},
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    assert "error" not in body

    result = body["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["serverInfo"] == {"name": "meho-backplane", "version": __version__}
    # T1 advertises an **empty** capabilities envelope — see
    # `ServerCapabilities` docstring + `_initialize` for rationale.
    # Advertising `tools` / `resources` ahead of T3 (#248) registering
    # the corresponding handlers would tell a spec-conforming client
    # it can call `tools/list`, which would then `-32601`. T3 flips the
    # envelopes back on paired with the dispatch-table additions.
    assert result["capabilities"] == {}


def test_initialize_without_protocol_version_returns_invalid_params(
    client: TestClient,
) -> None:
    """``initialize`` missing required ``protocolVersion`` → -32602."""
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "initialize",
            "params": {"capabilities": {}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 2
    assert body["error"]["code"] == INVALID_PARAMS
    assert "result" not in body


# ---------------------------------------------------------------------------
# notifications/initialized
# ---------------------------------------------------------------------------


def test_notifications_initialized_returns_202_with_empty_body(
    client: TestClient,
) -> None:
    """The post-initialize notification: 202 Accepted, no body."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
    )

    assert response.status_code == 202
    assert response.content == b""


def test_notifications_initialized_with_extraneous_id_still_returns_202(
    client: TestClient,
) -> None:
    """Buggy clients that include an id on a ``notifications/*`` method
    still get 202 — the method semantics are spec-defined as
    notification-only regardless of the envelope's id field.
    """
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "notifications/initialized",
        },
    )

    assert response.status_code == 202
    assert response.content == b""


# ---------------------------------------------------------------------------
# Unknown method routing
# ---------------------------------------------------------------------------


def test_unknown_method_returns_method_not_found_for_request(
    client: TestClient,
) -> None:
    """A request-shaped call to an unregistered method → -32601."""
    response = _post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/list",  # T3 (#248) registers; not yet present
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 3
    assert body["error"]["code"] == METHOD_NOT_FOUND
    assert "tools/list" in body["error"]["message"]


def test_unknown_notification_returns_202(client: TestClient) -> None:
    """Notification (no id) to an unregistered method → 202, dropped."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "method": "completely_unknown_method"},
    )

    assert response.status_code == 202
    assert response.content == b""


# ---------------------------------------------------------------------------
# JSON-RPC envelope errors
# ---------------------------------------------------------------------------


def test_parse_error_on_invalid_json(client: TestClient) -> None:
    """Non-JSON body → -32700, id=null per JSON-RPC §5."""
    response = client.post(
        "/mcp",
        content=b"this is not json",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] is None
    assert body["error"]["code"] == PARSE_ERROR


def test_empty_body_returns_parse_error(client: TestClient) -> None:
    """Empty body → -32700 with an explicit "empty body" message.

    Caught explicitly so the error message is informative rather than
    the default "Expecting value" from :func:`json.loads`.
    """
    response = client.post(
        "/mcp",
        content=b"",
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == PARSE_ERROR
    assert "empty" in body["error"]["message"]


def test_array_body_returns_invalid_request(client: TestClient) -> None:
    """JSON-RPC batch arrays are unsupported in T1 → -32600."""
    response = _post_mcp(
        client,
        [{"jsonrpc": "2.0", "id": 1, "method": "ping"}],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] is None
    assert body["error"]["code"] == INVALID_REQUEST


def test_wrong_jsonrpc_version_returns_invalid_request(client: TestClient) -> None:
    """Envelope with ``jsonrpc`` != "2.0" → -32600.

    The id can be echoed back when it's syntactically parseable; the
    dispatcher's best-effort id extractor preserves it so the client
    can correlate the failure with the in-flight request.
    """
    response = _post_mcp(
        client,
        {"jsonrpc": "1.0", "id": 4, "method": "ping"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 4
    assert body["error"]["code"] == INVALID_REQUEST


def test_request_missing_method_returns_invalid_request(client: TestClient) -> None:
    """Envelope without a ``method`` field → -32600."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 5},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 5
    assert body["error"]["code"] == INVALID_REQUEST


@pytest.mark.parametrize("bad_id", [True, False])
def test_bool_id_is_rejected_as_invalid_request(
    client: TestClient,
    bad_id: bool,
) -> None:
    """``bool`` ids violate JSON-RPC §4.1.2 (Number/String/NULL only).

    ``bool`` subclasses ``int`` in Python, so without an explicit reject
    Pydantic would coerce ``True`` → ``1`` / ``False`` → ``0`` and echo
    that integer in the response, breaking the client's id correlation.
    The field validator on :class:`JsonRpcRequest.id` and the bool-
    short-circuit in :func:`_coerce_request_id` both gate this.
    """
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": bad_id, "method": "ping"},
    )

    assert response.status_code == 200
    body = response.json()
    # The error response carries id=null because the raw id was not a
    # valid JSON-RPC id; echoing the bool would propagate the bug.
    assert body["id"] is None
    assert body["error"]["code"] == INVALID_REQUEST


# ---------------------------------------------------------------------------
# ping utility
# ---------------------------------------------------------------------------


def test_ping_returns_empty_result(client: TestClient) -> None:
    """``ping`` returns ``result: {}``."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 7, "method": "ping"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["result"] == {}
    assert "error" not in body


# ---------------------------------------------------------------------------
# T1 auth contract — no Bearer required
# ---------------------------------------------------------------------------


def test_t1_has_no_auth_unauthenticated_call_succeeds(client: TestClient) -> None:
    """AC: "T1 has NO auth yet — every request currently succeeds."

    Calling ``/mcp`` with no ``Authorization`` header must still
    succeed in T1. T2 (#247) is what adds the OAuth-RS chain on top.
    """
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 8, "method": "ping"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["result"] == {}


# ---------------------------------------------------------------------------
# Wire shape: result and error are mutually exclusive on the wire
# ---------------------------------------------------------------------------


def test_success_response_omits_error_member(client: TestClient) -> None:
    """Per JSON-RPC §5, a successful response MUST NOT include ``error``."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 9, "method": "ping"},
    )

    body = response.json()
    assert "result" in body
    assert "error" not in body


def test_error_response_omits_result_member(client: TestClient) -> None:
    """Per JSON-RPC §5, an error response MUST NOT include ``result``."""
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 10, "method": "no_such_method"},
    )

    body = response.json()
    assert "error" in body
    assert "result" not in body


# ---------------------------------------------------------------------------
# MCP-Protocol-Version header validation
# ---------------------------------------------------------------------------


def test_protocol_version_header_matching_supported_is_accepted(
    client: TestClient,
) -> None:
    """Header present + matching ``PROTOCOL_VERSION`` → request proceeds normally."""
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 100, "method": "ping"},
        headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 100
    assert body["result"] == {}


def test_protocol_version_header_unsupported_returns_400(
    client: TestClient,
) -> None:
    """Header present + unsupported value → HTTP 400 per spec MUST.

    MCP 2025-06-18 §Protocol Version Header: "If the server receives a
    request with an invalid or unsupported `MCP-Protocol-Version`, it
    MUST respond with `400 Bad Request`."
    """
    response = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 101, "method": "ping"},
        headers={"MCP-Protocol-Version": "1999-01-01"},
    )

    assert response.status_code == 400
    body = response.json()
    assert body["id"] == 101
    assert body["error"]["code"] == INVALID_REQUEST
    assert "1999-01-01" in body["error"]["message"]


def test_protocol_version_header_absent_is_accepted_transitionally(
    client: TestClient,
) -> None:
    """Header absent on non-initialize → accepted (transitional lenience).

    Spec SHOULD-assume-2025-03-26 doesn't help us — v0.2 doesn't support
    that revision. T1 accepts header-absent so clients that don't yet
    emit it aren't broken. T6 (#251) will pin the strict-mode contract.
    """
    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 102, "method": "ping"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == 102


def test_initialize_does_not_require_protocol_version_header(
    client: TestClient,
) -> None:
    """``initialize`` is exempt — clients don't know the version yet.

    Per spec, the header is required on "all subsequent requests" after
    initialize. The initialize call itself happens before negotiation,
    so any header value (or its absence) is accepted on that call.
    """
    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 103,
            "method": "initialize",
            "params": {"protocolVersion": PROTOCOL_VERSION},
        },
        # Deliberately wrong header — would normally trigger 400 but
        # initialize is exempt.
        headers={"MCP-Protocol-Version": "1999-01-01"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 103
    assert body["result"]["protocolVersion"] == PROTOCOL_VERSION


# ---------------------------------------------------------------------------
# Method-not-allowed for non-POST verbs (transport contract)
# ---------------------------------------------------------------------------


def test_get_on_mcp_endpoint_returns_405(client: TestClient) -> None:
    """The Streamable HTTP transport allows clients to GET ``/mcp`` to
    open an SSE stream (§"Listening for Messages from the Server").
    T1 doesn't implement the SSE branch; FastAPI auto-replies 405 to GET
    on a POST-only route, which is the spec's allowed fallback ("the
    server ... return HTTP 405 Method Not Allowed").
    """
    response = client.get("/mcp")
    assert response.status_code == 405


# ---------------------------------------------------------------------------
# register_method duplicate-registration guard
# ---------------------------------------------------------------------------


def test_register_method_rejects_duplicate_registration() -> None:
    """``register_method`` is a programmer-error gate, not a network one.

    Test isolation: ``register_method`` raises *before* mutating
    ``_DISPATCH`` (the duplicate check runs ahead of the dict
    assignment), so a passing run leaves no residue in the module-
    level registry. No fixture-level cleanup is needed.
    """
    from meho_backplane.mcp.server import register_method

    async def _dummy(_params: dict[str, Any] | None) -> dict[str, Any]:
        return {}

    with pytest.raises(RuntimeError, match="already registered"):
        register_method("initialize", _dummy)


# ---------------------------------------------------------------------------
# Internal error path
# ---------------------------------------------------------------------------


def test_handler_exception_becomes_internal_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that raises an unexpected exception → -32603.

    Exercises the generic ``except Exception`` arm of the dispatcher.
    Registers a one-shot handler via ``monkeypatch`` on the module-level
    ``_DISPATCH`` dict so the test doesn't pollute the global registry.
    """
    from meho_backplane.mcp import server as server_module

    async def _boom(_params: dict[str, Any] | None) -> dict[str, Any]:
        raise ValueError("boom")

    monkeypatch.setitem(server_module._DISPATCH, "test_boom", _boom)

    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 11, "method": "test_boom"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 11
    assert body["error"]["code"] == INTERNAL_ERROR


def test_handler_returning_non_dict_scalar_becomes_internal_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler that returns a non-dict / non-BaseModel value → -32603.

    This is the load-bearing handler-bug guard at the end of
    ``mcp_dispatch``: if a handler returns a list, string, int, or any
    other shape that isn't a JSON-RPC ``result`` body, the dispatcher
    must convert that to INTERNAL_ERROR rather than emit a wire-broken
    envelope. ``test_handler_exception_becomes_internal_error`` covers
    the ``except Exception`` arm; this case is the "handler returned
    successfully but with the wrong shape" arm.
    """
    from meho_backplane.mcp import server as server_module

    async def _list_returner(_params: dict[str, Any] | None) -> list[int]:
        # Plausible handler bug: forgetting to wrap the items in a
        # ``{"items": [...]}`` dict before returning.
        return [1, 2, 3]

    monkeypatch.setitem(server_module._DISPATCH, "test_bad_shape", _list_returner)

    response = _post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 12, "method": "test_bad_shape"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 12
    assert body["error"]["code"] == INTERNAL_ERROR
    assert "result" in body["error"]["message"].lower()
