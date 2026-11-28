# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, every audit row of a call names the kind of caller.

Whole-path runs through the real gate, then the test reads the audit row:

* a person in the web console (``POST /ui/operations/call``): ``person``;
* a person's token over REST: ``personal_agent``, the person's ``sub`` and
  the login client in the payload block ``caller``;
* a person's token over MCP: ``personal_agent``, and the block names the
  tool name the MCP client sent in ``initialize``, also with
  ``AUDIT_CLIENT_IDENTITY`` off;
* an agent token and a service token: ``service_agent``;
* a runner token: ``runner``;
* a row under MEHO's own ``system`` name: ``system``.

A waiting request stores the requester's kind in
``proposed_effect["caller_kind"]``, and the approval card names it. The
audit API sends ``caller_kind`` for a row that has one and leaves the field
out for an older row. With the switch off, nothing changes.

Fixture values are made up.
"""

from __future__ import annotations

import ast
import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditEventClass, AuditLog
from meho_backplane.main import app as main_app
from meho_backplane.memory.audit import write_internal_audit_row
from meho_backplane.settings import get_settings
from tests._oidc_jwt_helpers import make_rsa_keypair, mint_token, public_jwks
from tests._oidc_jwt_helpers import mock_discovery_and_jwks as mock_jwks
from tests.test_ui_operations import (
    _BACKPLANE_URL,
    _TENANT_A,
    _bff_env,
    _client_with_role_and_csrf,
    _csrf_headers,
    _register_recording_gh_connector,
    _seed_gh_target,
    _seed_ingested_op,
    _seed_tenant,
)

__all__ = ["_bff_env"]

_CONNECTOR = "gh-rest-3"
_TARGET = "gh-prod"
_READ_OP = "GET:/repos/{owner}/{repo}/labels"
_CAUTION_OP = "POST:/repos/{owner}/{repo}/labels"
_PARAMS: dict[str, Any] = {"owner": "example-org", "repo": "example-repo", "body": {}}
_PERSON = "example-person"
_CONSOLE_PERSON = "example-console-person"
_TOOL = "Example Tool"
_SRC = Path(__file__).resolve().parent.parent / "src" / "meho_backplane"


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    monkeypatch.delenv("AUDIT_CLIENT_IDENTITY", raising=False)
    get_settings.cache_clear()


def _seed() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    _register_recording_gh_connector()
    _seed_ingested_op(
        tenant_id=None,
        op_id=_READ_OP,
        method="GET",
        path=_READ_OP.split(":", 1)[1],
        safety_level="safe",
    )
    _seed_ingested_op(
        tenant_id=None,
        op_id=_CAUTION_OP,
        method="POST",
        path=_CAUTION_OP.split(":", 1)[1],
        safety_level="caution",
    )
    _seed_gh_target(tenant_id=_TENANT_A)


def _rows(path: str | None = None) -> list[AuditLog]:
    async def _read() -> list[AuditLog]:
        query = select(AuditLog).order_by(AuditLog.occurred_at)
        if path is not None:
            query = query.where(AuditLog.path == path)
        async with get_sessionmaker()() as session:
            return list((await session.execute(query)).scalars().all())

    return asyncio.run(_read())


def _one_row(path: str) -> AuditLog:
    rows = _rows(path)
    assert len(rows) == 1, [(row.path, row.caller_kind) for row in rows]
    return rows[0]


def _requests() -> list[ApprovalRequest]:
    async def _read() -> list[ApprovalRequest]:
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(ApprovalRequest))).scalars().all())

    return asyncio.run(_read())


class _Tokens:
    """Signed tokens for one made-up key, and the calls that send them."""

    def __init__(self) -> None:
        self.key = make_rsa_keypair("kid-audit-caller-kind")
        self.client = TestClient(main_app)

    def token(
        self,
        sub: str,
        *,
        kind: str | None = None,
        azp: str | None = "meho-cli",
        audience: str | None = None,
        runner_id: str | None = None,
    ) -> str:
        claims: dict[str, Any] = {}
        if azp is not None:
            claims["azp"] = azp
        if kind in ("agent", "service"):
            claims["client_id"] = sub
        elif kind is None:
            # A person's token comes from an interactive sign-in.
            claims["sid"] = "made-up-sign-in-session"
        return mint_token(
            self.key,
            sub=sub,
            tenant_id=str(_TENANT_A),
            tenant_role=TenantRole.OPERATOR.value,
            principal_kind=kind,
            runner_id=runner_id,
            audience=audience,
            extra_claims=claims,
        )

    def send(self, method: str, path: str, token: str, **kwargs: Any) -> Any:
        clear_jwks_cache()  # a console run of the same test signs with another key
        headers = {"Authorization": f"Bearer {token}", **kwargs.pop("headers", {})}
        with respx.mock(assert_all_called=False) as mock:
            mock_jwks(mock, public_jwks(self.key))
            return self.client.request(method, path, headers=headers, **kwargs)

    def call(self, op_id: str, token: str) -> dict[str, Any]:
        body = {"connector_id": _CONNECTOR, "op_id": op_id, "target": _TARGET, "params": _PARAMS}
        response = self.send("POST", "/api/v1/operations/call", token, json=body)
        assert response.status_code == 200, response.text
        result: dict[str, Any] = response.json()
        return result


@pytest.fixture
def tokens() -> _Tokens:
    return _Tokens()


def _console_run(op_id: str) -> str:
    """Run *op_id* in the web console as a person; return the run status."""
    clear_jwks_cache()
    client, mock, csrf = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_CONSOLE_PERSON, role=TenantRole.OPERATOR
    )
    form = {"connector_id": _CONNECTOR, "op_id": op_id, "target": _TARGET, "params": "{}"}
    form["params"] = '{"owner": "example-org", "repo": "example-repo", "body": {}}'
    with mock:
        response = client.post("/ui/operations/call", headers=_csrf_headers(csrf), data=form)
    assert response.status_code == 200, response.text
    status = re.search(r'data-run-status="([a-z_]+)"', response.text)
    assert status is not None, response.text
    return status.group(1)


# ------------------------------------------------------------ the switch on


def test_a_console_call_writes_person(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, on=True)
    _seed()

    assert _console_run(_READ_OP) == "ok"

    row = _one_row(_READ_OP)
    assert (row.operator_sub, row.caller_kind) == (_CONSOLE_PERSON, "person")
    assert "caller" not in row.payload


def test_a_person_token_writes_personal_agent_and_the_login_client(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens
) -> None:
    _switch(monkeypatch, on=True)
    _seed()

    token = tokens.token(_PERSON, azp="meho-cli")
    assert tokens.call(_READ_OP, token)["status"] == "ok"

    row = _one_row(_READ_OP)
    assert (row.operator_sub, row.caller_kind) == (_PERSON, "personal_agent")
    assert row.payload["caller"] == {"azp": "meho-cli", "tool": None}
    # AUDIT_CLIENT_IDENTITY is off: its own block is not written.
    assert "client" not in row.payload

    # The audit API names the person and the kind.
    answer = tokens.send("GET", f"/api/v1/audit/show/{row.id}", token)
    assert answer.status_code == 200, answer.text
    assert answer.json()["principal_sub"] == _PERSON
    assert answer.json()["caller_kind"] == "personal_agent"


def _mcp(tokens: _Tokens, token: str, body: dict[str, Any], session: str | None = None) -> Any:
    headers = {"Accept": "application/json, text/event-stream"}
    if session is not None:
        headers["Mcp-Session-Id"] = session
    response = tokens.send("POST", "/mcp", token, json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response


def test_an_mcp_call_writes_the_tool_name_the_client_sent(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens
) -> None:
    _switch(monkeypatch, on=True)
    monkeypatch.setenv("MCP_RESOURCE_URI", "")
    get_settings.cache_clear()
    _seed()
    token = tokens.token(_PERSON, azp="meho-mcp-code", audience=f"{_BACKPLANE_URL}/mcp")

    opened = _mcp(
        tokens,
        token,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": f"{_TOOL} ", "version": "1.0"},
            },
        },
    )
    session = opened.headers["Mcp-Session-Id"]
    arguments = {"connector_id": _CONNECTOR, "op_id": _READ_OP, "target": _TARGET}
    _mcp(
        tokens,
        token,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "call_operation", "arguments": {**arguments, "params": _PARAMS}},
        },
        session=session,
    )

    # The session's first row keeps the name, cleaned like every client text.
    opened_row = _one_row("/mcp/initialize")
    assert opened_row.caller_kind == "personal_agent"
    assert opened_row.payload["caller"] == {"azp": "meho-mcp-code", "tool": _TOOL}
    row = _one_row(_READ_OP)
    assert (row.caller_kind, str(row.agent_session_id)) == ("personal_agent", session)
    assert row.payload["caller"] == {"azp": "meho-mcp-code", "tool": _TOOL}
    assert "client" not in row.payload


@pytest.mark.parametrize("kind", ["agent", "service"])
def test_an_agent_and_a_service_token_write_service_agent(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens, kind: str
) -> None:
    _switch(monkeypatch, on=True)
    _seed()

    tokens.call(_READ_OP, tokens.token(f"{kind}:example", kind=kind))

    rows = [row for row in _rows() if row.operator_sub == f"{kind}:example"]
    assert rows
    assert {row.caller_kind for row in rows} == {"service_agent"}
    assert all("caller" not in row.payload for row in rows)


def test_a_runner_writes_runner(monkeypatch: pytest.MonkeyPatch, tokens: _Tokens) -> None:
    _switch(monkeypatch, on=True)
    _seed()

    token = tokens.token("runner:example", kind="runner", azp=None, runner_id=str(uuid.uuid4()))
    response = tokens.send("GET", "/api/v1/targets", token)
    assert response.status_code == 403, response.text

    rows = [row for row in _rows() if row.operator_sub == "runner:example"]
    assert rows
    assert {row.caller_kind for row in rows} == {"runner"}


def test_a_waiting_request_stores_the_kind_and_the_card_names_it(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens
) -> None:
    _switch(monkeypatch, on=True)
    _seed()

    result = tokens.call(_CAUTION_OP, tokens.token(_PERSON))
    assert result["status"] == "awaiting_approval"

    (request,) = _requests()
    assert request.proposed_effect["caller_kind"] == "personal_agent"
    assert _one_row("approval.request").caller_kind == "personal_agent"

    # Another approver sees the person named; the person sees "your".
    for approver, line in (
        (_CONSOLE_PERSON, "The personal agent of Alice asked."),
        (_PERSON, "Your personal agent asked."),
    ):
        clear_jwks_cache()
        client, mock, _ = _client_with_role_and_csrf(
            tenant_id=_TENANT_A, operator_sub=approver, role=TenantRole.TENANT_ADMIN
        )
        with mock:
            modal = client.get(f"/ui/approvals/{request.id}")
        assert modal.status_code == 200, modal.text
        assert "data-approval-caller-kind" in modal.text
        assert line in modal.text


def test_the_approvals_list_names_the_kind(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens
) -> None:
    _switch(monkeypatch, on=True)
    _seed()
    agent = "agent:example"
    tokens.call(_CAUTION_OP, tokens.token(agent, kind="agent"))
    assert _requests(), "the agent's call did not wait"

    clear_jwks_cache()
    client, mock, _ = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_CONSOLE_PERSON, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        page = client.get("/ui/approvals", headers={"HX-Request": "true"})
    assert page.status_code == 200, page.text
    assert f"The service agent {agent} asked." in page.text


# ----------------------------------------------------------- the switch off


def test_with_the_switch_off_nothing_changes(
    monkeypatch: pytest.MonkeyPatch, tokens: _Tokens
) -> None:
    _switch(monkeypatch, on=False)
    _seed()

    token = tokens.token(_PERSON)
    assert tokens.call(_READ_OP, token)["status"] == "ok"
    assert tokens.call(_CAUTION_OP, token)["status"] == "awaiting_approval"
    assert _console_run(_READ_OP) == "ok"

    rows = _rows()
    assert rows
    assert {row.caller_kind for row in rows} == {None}
    assert all("caller" not in row.payload for row in rows)
    (request,) = _requests()
    assert "caller_kind" not in request.proposed_effect

    # An older row: the audit API leaves the field out, it does not send null.
    answer = tokens.send("GET", f"/api/v1/audit/show/{_rows(_READ_OP)[0].id}", token)
    assert answer.status_code == 200, answer.text
    assert "caller_kind" not in answer.json()

    clear_jwks_cache()
    client, mock, _ = _client_with_role_and_csrf(
        tenant_id=_TENANT_A, operator_sub=_CONSOLE_PERSON, role=TenantRole.TENANT_ADMIN
    )
    with mock:
        modal = client.get(f"/ui/approvals/{request.id}")
    assert modal.status_code == 200, modal.text
    assert "data-approval-caller-kind" not in modal.text


# ------------------------------------------------------- MEHO's own rows


async def _system_row(sub: str) -> AuditLog:
    audit_id = await write_internal_audit_row(
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator_sub=sub,
        tenant_id=_TENANT_A,
        method="INTERNAL",
        path="example.prune",
        status_code=200,
        duration_ms=1.0,
        payload={},
    )
    async with get_sessionmaker()() as session:
        row = await session.get(AuditLog, audit_id)
    assert row is not None
    return row


@pytest.mark.asyncio
@pytest.mark.parametrize("sub", ["system", "system:example-retention"])
async def test_a_row_under_a_system_name_writes_system(
    monkeypatch: pytest.MonkeyPatch, sub: str
) -> None:
    _switch(monkeypatch, on=True)
    assert (await _system_row(sub)).caller_kind == "system"
    # A name that only looks like it gets nothing.
    assert (await _system_row("systemic-person")).caller_kind is None

    _switch(monkeypatch, on=False)
    assert (await _system_row(sub)).caller_kind is None


def test_every_audit_row_is_built_as_an_audit_log_object() -> None:
    """The kind of a ``system`` row is set when its ``AuditLog`` is built.

    A Core insert into ``audit_log`` would build no object, so such a row
    would get no kind. No source file may write one.
    """
    core_insert = re.compile(
        r"insert\(\s*AuditLog\b|AuditLog\.__table__|INSERT\s+INTO\s+audit_log", re.IGNORECASE
    )
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if core_insert.search(node.value):
                    found.append(f"{path.relative_to(_SRC)}:{node.lineno}")
            elif isinstance(node, ast.Call) and core_insert.search(ast.unparse(node.func) + "("):
                found.append(f"{path.relative_to(_SRC)}:{node.lineno}")
            elif (
                isinstance(node, ast.Call)
                and ast.unparse(node.func).endswith("insert")
                and node.args
                and ast.unparse(node.args[0]) == "AuditLog"
            ):
                found.append(f"{path.relative_to(_SRC)}:{node.lineno}")
    assert found == []
