# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for MCP ``_meta.broadcast_detail`` per-call opt-in.

Coverage matrix (Task #380 / G6.3-T3 acceptance criteria):

* ``_meta.broadcast_detail="full"`` is parsed out of the MCP
  ``tools/call`` params and threaded into the resolver via
  ``request_override``.
* ``_meta.broadcast_detail="aggregate"`` is logged at info under
  ``mcp_broadcast_detail_invalid_meta`` and dropped silently
  (opt-in only).
* Missing ``_meta`` entirely → resolver gets ``request_override=None``.
* ``_meta`` not-a-dict (operator sent a malformed envelope) → graceful
  None, no crash.
* Audit row gains ``broadcast_detail_origin`` + ``broadcast_detail_effective``;
  the broadcast event payload carries neither (audit-only metadata).

The unit-shaped tests directly exercise
:func:`meho_backplane.mcp.handlers._read_mcp_broadcast_detail` for the
parsing matrix; the integration-shaped test drives ``POST /mcp`` end
to end so the handler-side threading is verified.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.handlers import _read_mcp_broadcast_detail
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


async def _audit_rows() -> list[AuditLog]:
    """Read every ``audit_log`` row, ordered by ``occurred_at``."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).order_by(AuditLog.occurred_at),
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# _read_mcp_broadcast_detail -- parsing matrix
# ---------------------------------------------------------------------------


class TestReadMcpBroadcastDetail:
    """Defensive accessors keep the parser fail-open under operator error."""

    def test_full_value_passes_through(self) -> None:
        """The honored value is the literal ``"full"``."""
        assert _read_mcp_broadcast_detail({"_meta": {"broadcast_detail": "full"}}) == "full"

    def test_aggregate_value_is_dropped(self) -> None:
        """``"aggregate"`` is a "weaken via channel" request -- rejected silently."""
        assert _read_mcp_broadcast_detail({"_meta": {"broadcast_detail": "aggregate"}}) is None

    def test_random_string_is_dropped(self) -> None:
        """Typos / fuzzing inputs map to ``None`` without raising."""
        assert _read_mcp_broadcast_detail({"_meta": {"broadcast_detail": "vErBoSe"}}) is None

    def test_missing_meta_returns_none(self) -> None:
        """No ``_meta`` field at all -- the default for legacy clients."""
        assert _read_mcp_broadcast_detail({"name": "vault.kv.read"}) is None

    def test_missing_broadcast_detail_returns_none(self) -> None:
        """``_meta`` present but no ``broadcast_detail`` key."""
        assert _read_mcp_broadcast_detail({"_meta": {"other_key": "value"}}) is None

    def test_meta_not_dict_returns_none(self) -> None:
        """Malformed envelope (``_meta`` is a string) doesn't crash."""
        assert _read_mcp_broadcast_detail({"_meta": "this should be a dict"}) is None

    def test_meta_is_list_returns_none(self) -> None:
        """``_meta`` as a list (another malformed shape) -- graceful None."""
        assert _read_mcp_broadcast_detail({"_meta": ["broadcast_detail", "full"]}) is None

    def test_broadcast_detail_int_value_returns_none(self) -> None:
        """Wrong-type ``broadcast_detail`` (operator sent ``1``)."""
        assert _read_mcp_broadcast_detail({"_meta": {"broadcast_detail": 1}}) is None

    def test_empty_params_returns_none(self) -> None:
        assert _read_mcp_broadcast_detail({}) is None


# ---------------------------------------------------------------------------
# Integration -- POST /mcp threads the value into the audit row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_tools_call_audit_row_carries_origin_and_effective(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Without ``_meta.broadcast_detail``, audit row carries default origin + full effective.

    ``meho.status`` is a ``read`` op_class -- non-sensitive -- so the
    default detail is ``"full"``. No tenant rules, no override → origin
    is ``"default"``, effective is ``"full"``.
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
    )
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is False

    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    row = mcp_rows[0]
    assert row.payload["broadcast_detail_origin"] == "default"
    assert row.payload["broadcast_detail_effective"] == "full"


@pytest.mark.asyncio
async def test_mcp_tools_call_with_full_meta_does_not_change_non_sensitive_origin(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``_meta.broadcast_detail="full"`` on a non-sensitive op is a no-op for origin.

    The request_override branch only fires when ``op_class`` is in
    ``{credential_read, audit_query}``. ``meho.status`` is ``read``,
    so origin stays ``"default"``. Pins that the middleware/handler
    parsing path doesn't accidentally upgrade non-sensitive ops.
    Resolver-level "request_override upgrades sensitive class"
    coverage lives in :mod:`tests.test_broadcast_overrides_resolver`.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {
                "name": "meho.status",
                "arguments": {},
                "_meta": {"broadcast_detail": "full"},
            },
        },
    )
    assert response.status_code == 200
    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].payload["broadcast_detail_origin"] == "default"
    assert mcp_rows[0].payload["broadcast_detail_effective"] == "full"


@pytest.mark.asyncio
async def test_mcp_tools_call_with_malformed_meta_does_not_crash(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """Malformed ``_meta`` (not a dict) is gracefully tolerated.

    The handler still writes the audit row + publishes the broadcast
    event; the malformed value is silently dropped. The MCP fail-open
    contract for the publish path is preserved.
    """
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "meho.status",
                "arguments": {},
                "_meta": "this should be a dict",
            },
        },
    )
    assert response.status_code == 200
    rows = await _audit_rows()
    mcp_rows = [r for r in rows if r.method == "MCP"]
    assert len(mcp_rows) == 1
    assert mcp_rows[0].payload["broadcast_detail_origin"] == "default"
