# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One ``mcp.session_open`` audit row per new MCP session (#6209).

With ``AUDIT_CLIENT_IDENTITY`` on, a successful ``initialize`` that gets
a new session id writes one row with the client name and version the
tool sent, and the ``User-Agent`` header. A failed ``initialize``, a
notification, or a resumed session (the client sent its own id) writes
no row. With the switch off, nothing changes.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.audit_query import AuditQueryFilters, query_audit
from meho_backplane.auth.client_identity import clean_client_text, sid_hash
from meho_backplane.auth.operator import Operator, RoleGroup, TenantMembership, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.audit import SESSION_OPEN_OP_ID
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.schemas import INTERNAL_ERROR, PROTOCOL_VERSION
from meho_backplane.settings import get_settings
from tests.mcp_test_fixtures import (
    OPERATOR_TENANT_ID,
    build_operator,
    client_with_operator,  # noqa: F401 — pytest-discovered fixture
    isolated_registry,  # noqa: F401 — pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 — pytest-discovered autouse fixture
)


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_CLIENT_IDENTITY", "true")
    get_settings.cache_clear()


def _initialize(client_info: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}}
    if client_info is not None:
        params["clientInfo"] = client_info
    return {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}


async def _session_open_rows() -> list[AuditLog]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(select(AuditLog).where(AuditLog.path == "/mcp/initialize"))
        return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_switch_on_writes_one_row_linked_to_the_session(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, op = client_with_operator
    response = post_mcp(
        client,
        _initialize({"name": "claude-code", "version": "2.1.0"}),
        headers={"User-Agent": "claude-code/2.1.0"},
    )

    assert response.status_code == 200
    assert "result" in response.json()
    session_id = UUID(response.headers["mcp-session-id"])

    rows = await _session_open_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row.event_class == AuditEventClass.SECURITY_EVENT
    assert row.agent_session_id == session_id
    assert row.operator_sub == op.sub
    assert row.method == "MCP"
    assert row.status_code == 200
    assert row.payload["op_id"] == SESSION_OPEN_OP_ID
    assert row.payload["client_name"] == "claude-code"
    assert row.payload["client_version"] == "2.1.0"
    assert row.payload["user_agent"] == "claude-code/2.1.0"
    assert row.payload["self_declared"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_later_rows_of_the_session_share_its_session_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(client, _initialize({"name": "claude-code", "version": "2.1.0"}))
    session_id = response.headers["mcp-session-id"]
    post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "meho_status", "arguments": {}},
        },
        headers={"Mcp-Session-Id": session_id},
    )

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.agent_session_id == UUID(session_id)),
        )
        paths = {row.path for row in result.scalars().all()}
    assert {"/mcp/initialize", "/mcp/tools/call/meho_status"} <= paths


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_audit_query_finds_the_row_by_op_id(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """``meho audit query --op-id mcp.session_open`` filters on this ``op_id``."""
    client, _op = client_with_operator
    post_mcp(client, _initialize({"name": "claude-desktop", "version": "1.0"}))

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await query_audit(
            AuditQueryFilters(op_id=SESSION_OPEN_OP_ID),
            tenant_id=OPERATOR_TENANT_ID,
            session=session,
        )
    assert [r.op_id for r in result.rows] == [SESSION_OPEN_OP_ID]


def _client_for(operator: Operator) -> Iterator[TestClient]:
    """Yield a ``TestClient`` whose MCP login check returns *operator*."""

    async def _fake_verify() -> Operator:
        return operator

    app.dependency_overrides[verify_mcp_jwt_and_bind] = _fake_verify
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


@pytest.fixture
def cli_client() -> Iterator[TestClient]:
    """A token login from the command line client, with a sign-in session."""
    operator = build_operator(TenantRole.READ_ONLY).model_copy(
        update={"azp": "meho-cli", "sid": "sign-in-session-1"},
    )
    yield from _client_for(operator)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_row_holds_the_client_block(cli_client: TestClient) -> None:
    """The session row also names the login client, like every audit row."""
    post_mcp(cli_client, _initialize({"name": "claude-code", "version": "2.1.0"}))

    rows = await _session_open_rows()
    assert len(rows) == 1
    block = rows[0].payload["client"]
    assert block["azp"] == "meho-cli"
    assert block["channel"] == "token"
    assert block["sid_hash"] == sid_hash("sign-in-session-1")
    assert block["sid_hash"] is not None
    assert "sign-in-session-1" not in str(rows[0].payload)


@pytest.fixture
def unchosen_client() -> Iterator[TestClient]:
    """A login with several tenants that sent no ``Meho-Tenant`` header (#5239).

    Its ``tenant_id`` is only a placeholder: the first tenant.
    """
    operator = build_operator(TenantRole.READ_ONLY).model_copy(
        update={
            "tenants": (
                TenantMembership(
                    tenant_id=OPERATOR_TENANT_ID, name="first", group=RoleGroup.READERS
                ),
                TenantMembership(
                    tenant_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                    name="second",
                    group=RoleGroup.OPERATORS,
                ),
            ),
            "tenant_chosen": False,
        },
    )
    yield from _client_for(operator)


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_session_without_a_chosen_tenant_writes_no_row(
    unchosen_client: TestClient,
) -> None:
    """The placeholder tenant is never written to the audit log."""
    response = post_mcp(unchosen_client, _initialize({"name": "claude-code", "version": "2.1.0"}))

    assert response.status_code == 200
    assert "result" in response.json()
    assert response.headers.get("mcp-session-id")
    assert await _session_open_rows() == []
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.tenant_id == OPERATOR_TENANT_ID),
        )
        assert list(result.scalars().all()) == []


@pytest.mark.asyncio
async def test_switch_off_writes_no_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(client, _initialize({"name": "claude-code", "version": "2.1.0"}))

    assert response.status_code == 200
    assert response.headers.get("mcp-session-id")
    assert await _session_open_rows() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_long_and_control_character_texts_are_cleaned(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    post_mcp(
        client,
        _initialize({"name": "evil\x1b[31m\nname‮" + "x" * 500, "version": 7}),
        headers={"User-Agent": "agent\tUA"},
    )

    rows = await _session_open_rows()
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["client_name"] == ("evil[31mname" + "x" * 500)[:200]
    assert len(payload["client_name"]) == 200
    # A version that is not a string is not stored.
    assert payload["client_version"] is None
    assert payload["user_agent"] == "agentUA"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_separators_and_lone_surrogates_still_open_the_session(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    """U+2028, U+2029 and a lone surrogate are removed; the session still opens.

    The body is written by hand: a JSON encoder cannot write a lone
    surrogate as UTF-8, but the escape ``\\ud800`` is valid JSON text.
    """
    client, _op = client_with_operator
    body = (
        '{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": '
        '{"protocolVersion": "' + PROTOCOL_VERSION + '", "capabilities": {}, '
        '"clientInfo": {"name": "line\\u2028para\\u2029name", "version": "1.\\udfff0\\ud800"}}}'
    )
    response = client.post("/mcp", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 200
    assert "result" in response.json()
    assert response.headers.get("mcp-session-id")
    rows = await _session_open_rows()
    assert len(rows) == 1
    assert rows[0].payload["client_name"] == "lineparaname"
    # A low surrogate first and a high surrogate last are both alone
    # (no pair), so both are removed.
    assert rows[0].payload["client_version"] == "1.0"


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_missing_client_info_still_writes_the_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    post_mcp(client, _initialize(None))

    rows = await _session_open_rows()
    assert len(rows) == 1
    assert rows[0].payload["client_name"] is None
    assert rows[0].payload["client_version"] is None


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_failed_initialize_writes_no_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(
        client,
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"capabilities": {}}},
    )

    assert "error" in response.json()
    assert await _session_open_rows() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_notification_writes_no_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    envelope = _initialize({"name": "claude-code", "version": "2.1.0"})
    del envelope["id"]
    response = post_mcp(client, envelope)

    assert response.status_code == 202
    assert await _session_open_rows() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_resumed_session_writes_no_row(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    response = post_mcp(
        client,
        _initialize({"name": "claude-code", "version": "2.1.0"}),
        headers={"Mcp-Session-Id": "11111111-2222-3333-4444-555555555555"},
    )

    assert response.status_code == 200
    assert "result" in response.json()
    assert await _session_open_rows() == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("switch_on")
async def test_audit_write_failure_fails_initialize(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator

    async def _explode(**_kwargs: Any) -> None:
        raise RuntimeError("simulated audit-write failure")

    with patch("meho_backplane.mcp.server.write_mcp_session_open_row", side_effect=_explode):
        response = post_mcp(client, _initialize({"name": "claude-code", "version": "2.1.0"}))

    assert response.json()["error"]["code"] == INTERNAL_ERROR
    assert response.headers.get("mcp-session-id") is None


def test_clean_client_text_edge_inputs() -> None:
    assert clean_client_text(None) is None
    assert clean_client_text(42) is None
    assert clean_client_text("") is None
    assert clean_client_text("\x00\x07") is None
    assert clean_client_text("ok-name 1.0") == "ok-name 1.0"
    assert clean_client_text("é" * 300) == "é" * 200
    assert clean_client_text("a\u2028b\u2029c") == "abc"
    assert clean_client_text("a\ud800b\udfffc") == "abc"
    assert clean_client_text("\u2028\ud800") is None
    # A real character outside the basic plane is kept.
    assert clean_client_text("tool \U0001f680") == "tool \U0001f680"
