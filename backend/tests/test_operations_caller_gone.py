# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A caller that disconnects leaves ``caller_gone_at`` on the audit row (#3696).

A synchronous call keeps running when the client goes away. These tests
check three things:

* the watcher records the disconnect time, and only on ``http.disconnect``;
* a real ``dispatch()`` that finishes after the caller left writes
  ``payload["caller_gone_at"]`` on its audit row, and still returns ``ok``;
* the REST ``/call`` route and the MCP ``POST /mcp`` route both run the
  watcher around the work.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from sqlalchemy import select

import meho_backplane.api.v1.operations as operations_api
import meho_backplane.mcp.server as mcp_server
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.base import Connector
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.schemas import FingerprintResult, ProbeResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations import (
    dispatch,
    register_typed_operation,
    reset_dispatcher_caches,
)
from meho_backplane.operations.caller_gone import caller_gone_at, watch_caller
from meho_backplane.settings import get_settings

_TENANT = UUID("00000000-0000-0000-0000-00000000a0a0")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()


def _operator() -> Operator:
    return Operator(
        sub="op-test",
        name="Test Operator",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )


async def _disconnect() -> dict[str, Any]:
    return {"type": "http.disconnect"}


async def _never() -> dict[str, Any]:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")  # pragma: no cover


async def _wait_for_caller_gone() -> datetime:
    """Stand in for a long vendor call that is still running when the caller leaves."""
    for _ in range(200):
        gone_at = caller_gone_at()
        if gone_at is not None:
            return gone_at
        await asyncio.sleep(0.01)
    raise AssertionError("the watcher never saw the disconnect")


# ---------------------------------------------------------------------------
# The watcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watch_caller_records_disconnect_time() -> None:
    assert caller_gone_at() is None
    async with watch_caller(_disconnect):
        gone_at = await _wait_for_caller_gone()
        assert gone_at.tzinfo is not None
    # The mark belongs to the request only.
    assert caller_gone_at() is None


@pytest.mark.asyncio
async def test_watch_caller_stays_empty_while_caller_is_connected() -> None:
    async with watch_caller(_never):
        await asyncio.sleep(0.05)
        assert caller_gone_at() is None


@pytest.mark.asyncio
async def test_watch_caller_ignores_other_messages_and_stops() -> None:
    calls = 0

    async def _body_again() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"", "more_body": False}

    async with watch_caller(_body_again):
        await asyncio.sleep(0.05)
        assert caller_gone_at() is None
    # One read, then the watcher stops: no busy loop.
    assert calls == 1


# ---------------------------------------------------------------------------
# A real dispatch writes the mark on its audit row
# ---------------------------------------------------------------------------


class _NoOpConnector(Connector):
    product = "demo"
    version = "1.x"
    impl_id = "demo"

    async def fingerprint(self, target: Any, operator: Any = None) -> FingerprintResult:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> ProbeResult:  # type: ignore[override]
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


async def _module_slow_deploy_handler(
    operator: Operator,
    target: Any,
    params: dict[str, Any],
) -> dict[str, Any]:
    """A deploy that finishes only after the caller has gone."""
    if params.get("wait_for_caller_gone"):
        await _wait_for_caller_gone()
    return {"deployed": True}


class _FakeFingerprint:
    version: str | None = None


class _FakeTarget:
    def __init__(self) -> None:
        self.product = "demo"
        self.fingerprint = _FakeFingerprint()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = "demo-target"
        self.host = "demo.example.com"
        self.port = 443
        self.auth_model = "shared_service_account"


async def _register_deploy_op(op_id: str) -> None:
    embedding = AsyncMock()
    embedding.encode_one.return_value = [0.1] * 384
    embedding.encode.return_value = [[0.1] * 384]
    embedding.dimension = 384
    register_connector_v2(product="demo", version="", impl_id="", cls=_NoOpConnector)
    await register_typed_operation(
        product="demo",
        version="1.x",
        impl_id="demo",
        op_id=op_id,
        handler=_module_slow_deploy_handler,
        summary="Demo deploy.",
        description="Demo long deploy used by the caller-gone tests.",
        parameter_schema={"type": "object"},
        when_to_use=None,
        embedding_service=embedding,
        safety_level="safe",
    )


async def _rows_for(op_id: str) -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        return list(
            (await session.execute(select(AuditLog).where(AuditLog.path == op_id))).scalars().all()
        )


@pytest.mark.asyncio
async def test_dispatch_after_caller_left_marks_audit_row_and_still_finishes() -> None:
    op_id = "demo.vm.deploy_slow"
    await _register_deploy_op(op_id)

    async with watch_caller(_disconnect):
        result = await dispatch(
            operator=_operator(),
            connector_id="demo-1.x",
            op_id=op_id,
            target=_FakeTarget(),
            params={"wait_for_caller_gone": True},
        )

    # The vendor call was not cut short.
    assert result.status == "ok"
    rows = await _rows_for(op_id)
    assert len(rows) == 1
    assert rows[0].id == result.audit_id
    assert rows[0].payload["result_status"] == "ok"
    gone_at = datetime.fromisoformat(rows[0].payload["caller_gone_at"])
    assert gone_at.tzinfo is not None


@pytest.mark.asyncio
async def test_dispatch_with_connected_caller_has_no_mark() -> None:
    op_id = "demo.vm.deploy_fast"
    await _register_deploy_op(op_id)

    async with watch_caller(_never):
        result = await dispatch(
            operator=_operator(),
            connector_id="demo-1.x",
            op_id=op_id,
            target=_FakeTarget(),
            params={},
        )

    assert result.status == "ok"
    rows = await _rows_for(op_id)
    assert len(rows) == 1
    assert "caller_gone_at" not in rows[0].payload


# ---------------------------------------------------------------------------
# The REST and MCP routes run the watcher
# ---------------------------------------------------------------------------


async def _post_then_disconnect(app: FastAPI, path: str, body: dict[str, Any]) -> list[Any]:
    """Send one POST over raw ASGI, then report the client as gone."""
    raw = json.dumps(body).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(raw)).encode()),
        ],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
    }
    messages = [{"type": "http.request", "body": raw, "more_body": False}]

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        return {"type": "http.disconnect"}

    sent: list[Any] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


@pytest.mark.asyncio
async def test_rest_call_route_watches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[datetime] = []

    async def _slow_call_operation(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
        seen.append(await _wait_for_caller_gone())
        return {"status": "ok"}

    monkeypatch.setattr(operations_api, "call_operation", _slow_call_operation)
    app = FastAPI()
    app.include_router(operations_api.router)
    app.dependency_overrides[operations_api._require_operator.dependency] = _operator

    sent = await _post_then_disconnect(
        app,
        "/api/v1/operations/call",
        {"connector_id": "demo-1.x", "op_id": "demo.vm.deploy", "params": {}},
    )

    assert sent[0]["status"] == 200
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_mcp_route_watches_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[datetime] = []

    async def _slow_tool(operator: Operator, params: dict[str, Any] | None) -> dict[str, Any]:
        seen.append(await _wait_for_caller_gone())
        return {"ok": True}

    monkeypatch.setitem(mcp_server._DISPATCH, "test/slow", _slow_tool)
    app = FastAPI()
    app.include_router(mcp_server.router)
    app.dependency_overrides[verify_mcp_jwt_and_bind] = _operator

    sent = await _post_then_disconnect(
        app,
        "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "test/slow", "params": {}},
    )

    assert sent[0]["status"] == 200
    assert len(seen) == 1
