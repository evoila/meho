# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Audit rows and approvals name the login client (``AUDIT_CLIENT_IDENTITY``).

With the switch on, every audit writer adds a ``client`` block:
the login client (``azp``), the channel (``web`` or ``token``), the
principal kind and a short hash of the sign-in session (``sid``). An
approval request keeps the requester's block in its stored effect, and an
approval decision names the approver's client. With the switch off, no
row changes. No row ever holds the token, the raw ``sid`` or an address.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy import select

from meho_backplane.audit import _write_audit_row
from meho_backplane.auth.client_identity import (
    bind_request_client,
    build_client_block,
    request_client_block,
    reset_request_client,
    sid_hash,
    via_label,
)
from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditEventClass, AuditLog
from meho_backplane.main import app
from meho_backplane.mcp.audit import write_mcp_audit_row
from meho_backplane.operations._audit import write_audit_row
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import approve_request, create_pending_request
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import APPROVAL_CLIENT_ID as _APPROVAL_CLIENT
from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import ISSUER as _ISSUER
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks

_TENANT = uuid.UUID("00000000-0000-0000-0000-00000000c1d1")
_SID = "made-up-sign-in-session-1"
_SID_HASH = hashlib.sha256(_SID.encode()).hexdigest()[:16]
_PARAMS: dict[str, Any] = {"disk_gb": 40}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("KEYCLOAK_JWT_LEEWAY_SECONDS", "30")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("AUDIT_CLIENT_IDENTITY", raising=False)
    monkeypatch.delenv("KEYCLOAK_CLI_APPROVE_CLIENT_ID", raising=False)
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_request_client()
    yield
    reset_request_client()
    get_settings.cache_clear()
    clear_jwks_cache()


@pytest.fixture
def switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_CLIENT_IDENTITY", "true")
    get_settings.cache_clear()


def _operator(
    *,
    sub: str = "user-alice",
    azp: str | None = "meho-cli",
    kind: PrincipalKind = PrincipalKind.USER,
    web: bool = False,
    sid: str | None = _SID,
    role: TenantRole = TenantRole.OPERATOR,
) -> Operator:
    return Operator(
        sub=sub,
        name="Alice Example",
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        azp=azp,
        sid=sid,
        web_session=web,
        auth_time=int(time.time()),
        acr="1",
    )


#: The four ways in: the web console, the command line, an MCP tool and a
#: service login. Each gives its own channel and client.
_CHANNELS = [
    pytest.param(
        _operator(azp="meho-ui", web=True),
        {"azp": "meho-ui", "channel": "web", "principal_kind": "user"},
        id="web",
    ),
    pytest.param(
        _operator(azp="meho-cli"),
        {"azp": "meho-cli", "channel": "token", "principal_kind": "user"},
        id="cli",
    ),
    pytest.param(
        _operator(azp="meho-mcp"),
        {"azp": "meho-mcp", "channel": "token", "principal_kind": "user"},
        id="mcp",
    ),
    pytest.param(
        _operator(sub="svc-pipeline", azp="pipeline", kind=PrincipalKind.SERVICE, sid=None),
        {"azp": "pipeline", "channel": "token", "principal_kind": "service"},
        id="service",
    ),
]


async def _row(audit_id: uuid.UUID) -> AuditLog:
    async with get_sessionmaker()() as session:
        row = await session.get(AuditLog, audit_id)
    assert row is not None
    return row


async def _all_rows_text() -> str:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return json.dumps([row.payload for row in rows], default=str)


async def _write_rest_row(payload: dict[str, Any]) -> uuid.UUID:
    audit_id = uuid.uuid4()
    await _write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.LEGACY_REQUEST,
        operator_sub="user-alice",
        tenant_id=_TENANT,
        target_id=None,
        method="GET",
        path="/api/v1/targets",
        status_code=200,
        request_id=None,
        duration_ms=1.0,
        payload=payload,
    )
    return audit_id


async def _write_mcp_row(operator: Operator) -> uuid.UUID:
    return await write_mcp_audit_row(
        event_class=AuditEventClass.LEGACY_REQUEST,
        operator=operator,
        method="MCP",
        path="/mcp/tools/call/meho_status",
        status_code=200,
        duration_ms=1.0,
        payload={"op_id": "meho_status", "op_class": "read"},
    )


async def _write_dispatch_row(operator: Operator) -> uuid.UUID:
    audit_id = uuid.uuid4()
    descriptor = SimpleNamespace(
        op_id="demo.thing.get", source_kind="typed", product="demo", version="1.x", impl_id="demo"
    )
    await write_audit_row(
        audit_id=audit_id,
        event_class=AuditEventClass.OPERATION_EXECUTION,
        operator=operator,
        descriptor=descriptor,  # type: ignore[arg-type]
        target=None,
        params_hash="",
        result_status="ok",
        duration_ms=1.0,
    )
    return audit_id


async def _park(operator: Operator, **kwargs: Any) -> ApprovalRequest:
    async with get_sessionmaker()() as session:
        request = await create_pending_request(
            session,
            operator=operator,
            connector_id="vmware-rest-9.0",
            op_id="vm.create",
            target=None,
            params=_PARAMS,
            params_hash=compute_params_hash(_PARAMS),
            **kwargs,
        )
        await session.commit()
        return request


async def _decision_payload(request_id: uuid.UUID) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        rows = await session.execute(
            select(AuditLog.payload).where(AuditLog.path == "approval.decision")
        )
        for (payload,) in rows:
            if payload.get("approval_request_id") == str(request_id):
                return dict(payload)
    raise AssertionError("no approval.decision audit row")


# ---------------------------------------------------------------------------
# The helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("operator", "expected"), _CHANNELS)
def test_block_names_channel_client_and_kind(operator: Operator, expected: dict[str, str]) -> None:
    block = build_client_block(operator)
    assert {k: block[k] for k in expected} == expected
    assert set(block) == {"azp", "channel", "principal_kind", "sid_hash"}


def test_sid_is_kept_only_as_a_short_hash() -> None:
    block = build_client_block(_operator())
    assert block["sid_hash"] == _SID_HASH
    assert len(block["sid_hash"]) == 16
    assert _SID not in json.dumps(block)


def test_no_sid_and_no_azp_give_none() -> None:
    block = build_client_block(_operator(azp=None, sid=None))
    assert block["azp"] is None
    assert block["sid_hash"] is None
    assert sid_hash("") is None


def test_request_block_is_none_with_the_switch_off() -> None:
    bind_request_client(_operator())
    assert request_client_block() is None


def test_request_block_is_reset(switch_on: None) -> None:
    bind_request_client(_operator())
    assert request_client_block() is not None
    reset_request_client()
    assert request_client_block() is None


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"client": {"azp": "meho-cli", "channel": "token"}}, "meho-cli (token)"),
        ({"client": {"azp": "meho-ui", "channel": "web"}}, "meho-ui (web)"),
        ({"client": {"azp": None, "channel": "token"}}, "unknown client (token)"),
        ({"client": {"azp": "meho-cli"}}, "meho-cli"),
        ({"client": "meho-cli"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_via_label(payload: Any, expected: str | None) -> None:
    assert via_label(payload) == expected


# ---------------------------------------------------------------------------
# The three audit writers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("operator", "expected"), _CHANNELS)
async def test_writers_add_the_block_with_the_switch_on(
    switch_on: None, operator: Operator, expected: dict[str, str]
) -> None:
    bind_request_client(operator)
    rows = [
        await _row(await _write_rest_row({})),
        await _row(await _write_mcp_row(operator)),
        await _row(await _write_dispatch_row(operator)),
    ]
    for row in rows:
        client = row.payload["client"]
        assert {k: client[k] for k in expected} == expected
        assert client["sid_hash"] == (None if operator.sid is None else _SID_HASH)


@pytest.mark.asyncio
@pytest.mark.parametrize(("operator", "expected"), _CHANNELS)
async def test_writers_add_nothing_with_the_switch_off(
    operator: Operator, expected: dict[str, str]
) -> None:
    bind_request_client(operator)
    rest = await _row(await _write_rest_row({}))
    mcp = await _row(await _write_mcp_row(operator))
    dispatch = await _row(await _write_dispatch_row(operator))
    assert rest.payload == {}
    assert "client" not in mcp.payload
    assert "client" not in dispatch.payload


@pytest.mark.asyncio
async def test_rest_row_without_a_checked_login_has_no_block(switch_on: None) -> None:
    row = await _row(await _write_rest_row({}))
    assert row.payload == {}


@pytest.mark.asyncio
async def test_a_bound_payload_cannot_set_the_block(switch_on: None) -> None:
    operator = _operator(azp="meho-mcp")
    row = await _row(
        await write_mcp_audit_row(
            event_class=AuditEventClass.LEGACY_REQUEST,
            operator=operator,
            method="MCP",
            path="/mcp/tools/call/meho_status",
            status_code=200,
            duration_ms=1.0,
            payload={"op_id": "meho_status", "client": {"azp": "made-up"}},
        )
    )
    assert row.payload["client"]["azp"] == "meho-mcp"


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_stores_the_requester_client(switch_on: None) -> None:
    request = await _park(_operator(sub="op-requester", azp="meho-mcp"))
    client = request.proposed_effect["client"]
    assert client["azp"] == "meho-mcp"
    assert client["channel"] == "token"
    request_row = await _row(request.request_audit_id)
    assert request_row.payload["client"]["azp"] == "meho-mcp"


@pytest.mark.asyncio
async def test_request_stores_nothing_with_the_switch_off() -> None:
    request = await _park(_operator(sub="op-requester", azp="meho-mcp"))
    assert request.proposed_effect == {"op_id": "vm.create", "connector_id": "vmware-rest-9.0"}
    assert "client" not in (await _row(request.request_audit_id)).payload


@pytest.mark.asyncio
async def test_repark_for_an_earlier_requester_stores_no_reviewer_client(switch_on: None) -> None:
    reviewer = _operator(sub="op-reviewer", azp="meho-ui", web=True)
    request = await _park(
        reviewer, requester=("op-requester", None, "Requester", PrincipalKind.USER.value)
    )
    assert "client" not in request.proposed_effect


@pytest.mark.asyncio
async def test_console_approve_names_the_approver_client(switch_on: None) -> None:
    request = await _park(_operator(sub="op-requester", azp="meho-mcp"))
    reviewer = _operator(sub="op-reviewer", azp="meho-ui", web=True, role=TenantRole.TENANT_ADMIN)
    async with get_sessionmaker()() as session:
        await approve_request(session, request.id, operator=reviewer)
        await session.commit()

    payload = await _decision_payload(request.id)
    assert payload["approver_azp"] == "meho-ui"
    assert payload["approver_channel"] == "web"
    assert "approver_user_agent" not in payload
    assert payload["client"]["channel"] == "web"


@pytest.mark.asyncio
async def test_approve_with_the_switch_off_adds_no_field() -> None:
    request = await _park(_operator(sub="op-requester"))
    reviewer = _operator(sub="op-reviewer", azp="meho-ui", web=True, role=TenantRole.TENANT_ADMIN)
    async with get_sessionmaker()() as session:
        await approve_request(session, request.id, operator=reviewer, user_agent="meho-cli/1.0")
        await session.commit()

    payload = await _decision_payload(request.id)
    for key in ("approver_azp", "approver_channel", "approver_user_agent", "client"):
        assert key not in payload


@pytest.mark.asyncio
async def test_long_user_agent_is_cut(switch_on: None) -> None:
    request = await _park(_operator(sub="op-requester"))
    reviewer = _operator(sub="op-reviewer", role=TenantRole.TENANT_ADMIN)
    async with get_sessionmaker()() as session:
        await approve_request(session, request.id, operator=reviewer, user_agent="x" * 1000)
        await session.commit()

    agent = (await _decision_payload(request.id))["approver_user_agent"]
    assert agent == {"value": "x" * 256, "self_declared": True}


# ---------------------------------------------------------------------------
# A REST approve, end to end
# ---------------------------------------------------------------------------


@pytest.fixture
def _fake_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    from meho_backplane.api.v1 import approvals as approvals_module

    async def _resume(*, operator: Operator, request: ApprovalRequest, params: Any) -> Any:
        return SimpleNamespace(status="ok", op_id=request.op_id, result=None, error=None)

    monkeypatch.setattr(approvals_module, "_resume_dispatch_after_approval", _resume)


@pytest.fixture
async def _client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    key = make_rsa_keypair("kid-client-identity")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


def _approver_token(key: Any) -> str:
    return mint_token(
        key,
        sub="op-approver",
        tenant_role=TenantRole.TENANT_ADMIN.value,
        tenant_id=str(_TENANT),
        extra_claims={
            "azp": _APPROVAL_CLIENT,
            "acr": "1",
            "auth_time": int(time.time()),
            "sid": _SID,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["approve", "decide"])
async def test_rest_approve_names_the_approver_client(
    route: str,
    switch_on: None,
    _client: tuple[httpx.AsyncClient, Any],
    _fake_resume: None,
) -> None:
    ac, key = _client
    request = await _park(_operator(sub="op-requester", azp="meho-mcp"))
    token = _approver_token(key)
    body: dict[str, Any] = (
        {"params": _PARAMS} if route == "approve" else {"decision": "approved", "reason": ""}
    )

    response = await ac.post(
        f"/api/v1/approvals/{request.id}/{route}",
        json=body,
        headers={"Authorization": f"Bearer {token}", "User-Agent": "meho-cli/9.9"},
    )

    assert response.status_code == 200, response.text
    payload = await _decision_payload(request.id)
    assert payload["approver_azp"] == _APPROVAL_CLIENT
    assert payload["approver_channel"] == "token"
    assert payload["approver_user_agent"] == {"value": "meho-cli/9.9", "self_declared": True}
    assert payload["client"] == {
        "azp": _APPROVAL_CLIENT,
        "channel": "token",
        "principal_kind": "user",
        "sid_hash": _SID_HASH,
    }

    # The REST request row of the call carries the same block.
    async with get_sessionmaker()() as session:
        rest_rows = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.path == f"/api/v1/approvals/{request.id}/{route}"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rest_rows
    assert rest_rows[0].payload["client"]["azp"] == _APPROVAL_CLIENT

    # No token, no raw sign-in session id and no address in any row.
    text = await _all_rows_text()
    assert token not in text
    assert _SID not in text
    assert "127.0.0.1" not in text
