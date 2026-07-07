# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for MCP ``inputSchema`` ``format`` keyword enforcement.

The ``tools/call`` argument gate in :mod:`meho_backplane.mcp.handlers`
validates arguments with
``format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER``. Per
JSON Schema 2020-12, ``format`` is annotation-only unless a checker is
supplied — without it, keywords like ``format: uuid`` and
``format: date-time`` were collected but never asserted, so malformed
values sailed past the schema gate and surfaced from in-handler parsers
as JSON-RPC ``-32603`` "Internal error" instead of the
self-correctable ``-32602`` "Invalid params".

Coverage:

* Guard: ``uuid`` and ``date-time`` checkers are registered on
  :data:`jsonschema.Draft202012Validator.FORMAT_CHECKER`. The
  ``date-time`` registration only happens when ``rfc3339-validator``
  is importable, so this pins the dependency — dropping it would
  silently demote ``format: date-time`` back to an annotation.
* A malformed ``format: uuid`` argument → ``-32602`` (synthetic tool).
* A malformed ``format: date-time`` argument → ``-32602`` (synthetic
  tool).
* Well-formed values still dispatch to the handler (no false
  positives), including ``null`` on the nullable-UUID shape shared by
  the connector-admin tools
  (:data:`meho_backplane.mcp.tools._connector_shared._TENANT_ID_PROPERTY`
  — ``format`` applies to strings only, so ``null`` passes the type
  union untouched).
* Production wiring: ``meho.connector.review`` with a malformed
  ``tenant_id`` is rejected at the schema gate (``-32602``, message
  names the inputSchema) — previously that value reached
  ``_coerce_tenant_id``'s bare ``UUID(raw)`` re-parse and blew up as
  ``-32603``.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.schemas import INVALID_PARAMS
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)

# ---------------------------------------------------------------------------
# Format-checker registration guard
# ---------------------------------------------------------------------------


def test_uuid_and_datetime_checkers_registered() -> None:
    """Both formats the tool schemas declare are asserted, not annotated.

    ``uuid`` is checked natively by jsonschema; ``date-time`` requires
    the ``rfc3339-validator`` package and is registered only when it
    imports. If this test fails on ``date-time``, the dependency was
    dropped and every ``format: date-time`` keyword in the MCP tool
    schemas silently stopped being enforced.
    """
    checkers = jsonschema.Draft202012Validator.FORMAT_CHECKER.checkers
    assert "uuid" in checkers
    assert "date-time" in checkers


# ---------------------------------------------------------------------------
# Synthetic-tool fixtures
# ---------------------------------------------------------------------------


def _register_format_probe_tool() -> dict[str, list[dict[str, Any]]]:
    """Register a synthetic tool whose schema declares both formats.

    ``ref`` mirrors the nullable-UUID shape the connector-admin tools
    share (``type: [string, null]`` + ``format: uuid``); ``at`` is a
    required RFC 3339 timestamp. Returns the call-log the stub handler
    appends to, so tests can assert whether dispatch happened.
    """
    calls: dict[str, list[dict[str, Any]]] = {"args": []}

    async def _stub(_op: Operator, args: dict[str, Any]) -> dict[str, Any]:
        calls["args"].append(args)
        return {"ok": True}

    register_mcp_tool(
        ToolDefinition(
            name="test.format_probe",
            description="Synthetic tool asserting format enforcement",
            inputSchema={
                "type": "object",
                "properties": {
                    "ref": {"type": ["string", "null"], "format": "uuid"},
                    "at": {"type": "string", "format": "date-time"},
                },
                "required": ["at"],
                "additionalProperties": False,
            },
            # The shared fixture operator defaults to READ_ONLY; the
            # role gate runs before schema validation, so the probe
            # must be callable for the format checks to be reached.
            required_role=TenantRole.READ_ONLY,
        ),
        _stub,
    )
    return calls


def _call_format_probe(client: TestClient, arguments: dict[str, Any]) -> Any:
    """POST a ``tools/call`` for the synthetic probe tool."""
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "test.format_probe", "arguments": arguments},
        },
    )
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Rejection: malformed values fail at the schema gate as -32602
# ---------------------------------------------------------------------------


def test_malformed_uuid_rejected_as_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A ``format: uuid`` argument with a non-UUID string → ``-32602``.

    The stub handler recording zero calls proves the rejection happened
    at the schema gate, before dispatch — not inside the handler.
    """
    client, _op = client_with_operator
    calls = _register_format_probe_tool()

    body = _call_format_probe(
        client,
        {"ref": "not-a-uuid", "at": "2026-07-06T12:00:00Z"},
    )

    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputschema" in body["error"]["message"].lower()
    assert "uuid" in body["error"]["message"].lower()
    assert calls["args"] == []


def test_malformed_datetime_rejected_as_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A ``format: date-time`` argument with a non-RFC-3339 value → ``-32602``."""
    client, _op = client_with_operator
    calls = _register_format_probe_tool()

    body = _call_format_probe(client, {"at": "yesterday at noon"})

    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputschema" in body["error"]["message"].lower()
    assert "date-time" in body["error"]["message"].lower()
    assert calls["args"] == []


@pytest.mark.parametrize(
    "bad_datetime",
    [
        "2026-07-06",  # date only — RFC 3339 date-time needs the time part
        "2026-07-06T12:00:00",  # missing timezone offset
        "2026-13-40T99:99:99Z",  # syntactically shaped, semantically invalid
    ],
)
def test_non_rfc3339_shapes_rejected(
    client_with_operator: tuple[TestClient, Operator],
    bad_datetime: str,
) -> None:
    """Near-miss timestamp shapes are all rejected at the gate."""
    client, _op = client_with_operator
    _register_format_probe_tool()

    body = _call_format_probe(client, {"at": bad_datetime})

    assert body["error"]["code"] == INVALID_PARAMS


# ---------------------------------------------------------------------------
# No false positives: well-formed values still dispatch
# ---------------------------------------------------------------------------


def test_wellformed_values_dispatch_to_handler(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """A valid UUID + RFC 3339 timestamp pass the gate and reach the handler."""
    client, _op = client_with_operator
    calls = _register_format_probe_tool()

    arguments = {
        "ref": "00000000-0000-0000-0000-00000000a0a0",
        "at": "2026-07-06T12:00:00+00:00",
    }
    body = _call_format_probe(client, arguments)

    assert "error" not in body
    assert calls["args"] == [arguments]


def test_null_on_nullable_uuid_shape_dispatches(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``null`` passes the ``[string, null]`` + ``format: uuid`` union.

    ``format`` constrains strings only; enabling the checker must not
    reject the explicit-``null`` tenant-scope convention the
    connector-admin tools document.
    """
    client, _op = client_with_operator
    calls = _register_format_probe_tool()

    body = _call_format_probe(client, {"ref": None, "at": "2026-07-06T12:00:00Z"})

    assert "error" not in body
    assert len(calls["args"]) == 1


# ---------------------------------------------------------------------------
# Production wiring: a real tool's format keyword is load-bearing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "client_with_operator",
    [TenantRole.OPERATOR],
    indirect=True,
)
def test_connector_review_malformed_tenant_id_is_invalid_params(
    client_with_operator: tuple[TestClient, Operator],
) -> None:
    """``meho.connector.review`` with a malformed ``tenant_id`` → ``-32602``.

    Exercises the real registered tool schema
    (``_TENANT_ID_PROPERTY``'s ``format: uuid``): the schema gate now
    rejects the value before ``_coerce_tenant_id``'s ``UUID(raw)``
    re-parse can raise, so the caller sees "Invalid params" rather
    than an internal error. No service/DB stubbing is needed —
    rejection happens pre-dispatch.
    """
    client, _op = client_with_operator

    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.connector.review",
                "arguments": {
                    "connector_id": "vault-1.x",
                    "tenant_id": "not-a-uuid",
                },
            },
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["error"]["code"] == INVALID_PARAMS
    assert "inputschema" in body["error"]["message"].lower()
