# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the satellite-runner work-item executor (#2497).

Covers the four executor contracts: a real ``safety_level="safe"`` op
executes and returns a structured result; a non-``safe`` op is refused
without invocation; a ``handler_ref`` outside the connector tree is
refused fail-closed; and a handler that raises becomes a structured
error result rather than a raised tick error.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.runner.executor import execute_work_item
from meho_backplane.runner.wire import RunnerPrincipal, RunnerWorkItem

_ALLOWLIST_ENV = "MEHO_NETDIAG_PROBE_ALLOWLIST"
_NET_TCP_CHECK_REF = "meho_backplane.connectors.net.ops.net_tcp_check"


def _principal() -> RunnerPrincipal:
    return RunnerPrincipal(
        sub="runner-svc",
        tenant_id=uuid.uuid4(),
        tenant_role=TenantRole.READ_ONLY,
    )


def _tcp_check_item(*, host: str, port: int, **overrides: object) -> RunnerWorkItem:
    item = RunnerWorkItem(
        check_ref="chk-1",
        op_id="net.tcp_check",
        product="net",
        version="1.x",
        impl_id="net-probe",
        handler_ref=_NET_TCP_CHECK_REF,
        params={"host": host, "port": port},
        safety_level="safe",
        principal=_principal(),
    )
    return item.model_copy(update=overrides) if overrides else item


async def test_safe_op_executes_and_returns_structured_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    server = await asyncio.start_server(lambda _r, w: w.close(), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        item = _tcp_check_item(host="127.0.0.1", port=port)
        result = await execute_work_item(item)
    finally:
        server.close()
        await server.wait_closed()

    assert result.status == "ok"
    assert result.op_id == "net.tcp_check"
    assert result.check_ref == "chk-1"
    assert result.error is None
    assert result.result is not None
    # Structured reachability payload from the net.tcp_check handler.
    assert result.result["connected"] is True
    assert result.result["host"] == "127.0.0.1"
    # Runner-generated dedup id.
    assert len(result.result_uid) == 32


async def test_non_safe_op_is_refused_without_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An allowlisted host would let the probe succeed *if* it ran — proving
    # the refusal short-circuits before the handler is ever invoked.
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    item = _tcp_check_item(host="127.0.0.1", port=9, safety_level="caution", check_ref="chk-2")

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "caution" in (result.error or "")


async def test_out_of_tree_handler_ref_is_refused_fail_closed() -> None:
    item = _tcp_check_item(host="127.0.0.1", port=9, handler_ref="os.system", check_ref="chk-3")

    result = await execute_work_item(item)

    assert result.status == "refused"
    assert result.result is None
    assert "os.system" in (result.error or "")


async def test_handler_exception_becomes_structured_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_ALLOWLIST_ENV, "127.0.0.1")
    # Omit the required ``port`` param so net_tcp_check raises KeyError.
    item = _tcp_check_item(host="127.0.0.1", port=9, check_ref="chk-4")
    item = item.model_copy(update={"params": {"host": "127.0.0.1"}})

    result = await execute_work_item(item)

    assert result.status == "error"
    assert result.result is None
    assert "KeyError" in (result.error or "")
