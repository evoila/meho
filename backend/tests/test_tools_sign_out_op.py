# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the ``tools.sign_out`` typed op on ``tools-admin-1.x``.

A tenant admin signs out one tool kind of another person. A fake Keycloak
admin API (respx) keeps the people of MEHO's sign-in realm, their role
groups and their logins.

* **Registration** -- ``caution`` with ``requires_approval``, a
  ``tenant_admin`` role floor, and a check that only a person asks.
* **Approval** -- every call waits; the card names the person by id; an
  agent and an ``operator`` are refused; a tenant admin approves and the
  sign-out runs.
* **Run** -- only the chosen tool of the chosen person ends; the audit row
  names the admin, the person and the login client. The approved run signs
  out the id on the card, never a person found again by name.
* **Bad cases** -- a person of another tenant, an unknown person, a card
  without a person, the switch off, the web console, and a Keycloak
  failure are errors, and no "signed out" row is written.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.auth.tool_sign_out import TOOL_SIGN_OUT_AUDIT_PATH
from meho_backplane.connectors.tools.ops import (
    NEEDS_PERSON,
    NO_CARD_USER,
    TOOLS_CONNECTOR_ID,
    TOOLS_SIGN_OUT_OP_ID,
    register_tools_sign_out_operation,
    tools_sign_out_requester_refusal,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import approved_preview_var
from meho_backplane.operations.approval_queue import (
    ApprovalRoleFloorError,
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.settings import get_settings

from ._oidc_jwt_helpers import AUDIENCE as _AUDIENCE
from ._oidc_jwt_helpers import DEFAULT_TENANT_ID
from ._oidc_jwt_helpers import ISSUER as _ISSUER

_TENANT = uuid.UUID(DEFAULT_TENANT_ID)
_OTHER_TENANT = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ADMIN_URL = "https://keycloak.test/admin/realms/meho"
_ALICE = "5d0c1a52-0000-4000-8000-00000000aaaa"
_BOB = "5d0c1a52-0000-4000-8000-00000000bbbb"
_CAROL = "5d0c1a52-0000-4000-8000-00000000cccc"
#: A person from a linked user directory: the id is not a UUID.
_DORA = "f:3c5e7a90-directory:dora"
_DESKTOP = "meho-mcp-desktop"
_CODE = "meho-mcp-code"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", _ISSUER)
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", _AUDIENCE)
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("KEYCLOAK_CLI_CLIENT_ID", "meho-cli")
    monkeypatch.setenv("KEYCLOAK_ADMIN_URL", _ADMIN_URL)
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_ID", "meho-admin")
    monkeypatch.setenv("KEYCLOAK_ADMIN_CLIENT_SECRET", "made-up-admin-secret")
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "true")
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    clear_jwks_cache()
    reset_dispatcher_caches()


@pytest_asyncio.fixture(autouse=True)
async def _registered_op(_settings_env: None) -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_tools_sign_out_operation(embedding_service=service)
    yield


class _FakeRealm:
    """People, role groups and logins of MEHO's sign-in realm."""

    def __init__(self, router: respx.MockRouter, *, fail_sessions: bool = False) -> None:
        self.people: dict[str, tuple[str, uuid.UUID]] = {
            _ALICE: ("alice", _TENANT),
            _BOB: ("bob", _TENANT),
            _CAROL: ("carol", _OTHER_TENANT),
            _DORA: ("dora", _TENANT),
        }
        self.offline: set[tuple[str, str]] = {
            (_ALICE, _DESKTOP),
            (_ALICE, _CODE),
            (_BOB, _DESKTOP),
            (_CAROL, _DESKTOP),
            (_DORA, _DESKTOP),
        }
        router.post(url__regex=r".*/protocol/openid-connect/token$").mock(
            return_value=httpx.Response(200, json={"access_token": "admin-tok"})
        )
        router.get(url__regex=rf"^{_ADMIN_URL}/users/[^/]+/groups").mock(side_effect=self._groups)
        router.get(url__regex=rf"^{_ADMIN_URL}/users/[^/]+/sessions$").mock(
            return_value=httpx.Response(500) if fail_sessions else httpx.Response(200, json=[])
        )
        router.get(url__regex=rf"^{_ADMIN_URL}/users/[^/?]+$").mock(side_effect=self._user)
        router.get(url__regex=rf"^{_ADMIN_URL}/users(\?.*)?$").mock(side_effect=self._search)
        router.delete(url__regex=rf"^{_ADMIN_URL}/users/[^/]+/consents/[^/]+$").mock(
            side_effect=self._delete_consent
        )

    def _user(self, request: httpx.Request) -> httpx.Response:
        user_id = unquote(request.url.path.split("/")[-1])
        if user_id not in self.people:
            return httpx.Response(404)
        return httpx.Response(200, json={"id": user_id, "username": self.people[user_id][0]})

    def _search(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("username", "")
        return httpx.Response(
            200,
            json=[
                {"id": user_id, "username": username}
                for user_id, (username, _) in self.people.items()
                if username == name
            ],
        )

    def _groups(self, request: httpx.Request) -> httpx.Response:
        user_id = unquote(request.url.path.split("/")[-2])
        tenant = self.people[user_id][1]
        return httpx.Response(
            200,
            json=[
                {
                    "id": "g",
                    "name": "MEHO-Operators",
                    "attributes": {"meho_tenant": [f"{tenant}:main:Operators"]},
                }
            ],
        )

    def _delete_consent(self, request: httpx.Request) -> httpx.Response:
        parts = request.url.path.split("/")
        pair = (unquote(parts[-3]), unquote(parts[-1]))
        if pair not in self.offline:
            return httpx.Response(404)
        self.offline.discard(pair)
        return httpx.Response(204)


def _operator(
    sub: str = "p-admin",
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    role: TenantRole = TenantRole.TENANT_ADMIN,
    web_session: bool = False,
) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="fixture-jwt-not-real",
        tenant_id=_TENANT,
        tenant_role=role,
        principal_kind=kind,
        web_session=web_session,
    )


def _args(user: str, tool: str = "claude-desktop") -> dict[str, Any]:
    return {
        "connector_id": TOOLS_CONNECTOR_ID,
        "op_id": TOOLS_SIGN_OUT_OP_ID,
        "params": {"user": user, "tool": tool},
    }


async def _run(user: str, tool: str = "claude-desktop", operator: Operator | None = None) -> Any:
    return await dispatch(
        operator=operator or _operator(),
        connector_id=TOOLS_CONNECTOR_ID,
        op_id=TOOLS_SIGN_OUT_OP_ID,
        target=None,
        params={"user": user, "tool": tool},
        _approved=True,
    )


async def _sign_out_rows() -> list[AuditLog]:
    async with get_sessionmaker()() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    return [row for row in rows if row.path == TOOL_SIGN_OUT_AUDIT_PATH]


async def _approval_rows() -> list[ApprovalRequest]:
    async with get_sessionmaker()() as session:
        return list((await session.execute(select(ApprovalRequest))).scalars().all())


def _error_text(result: Any) -> str:
    return f"{result.error} {result.extras}"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


async def test_registered_as_a_caution_change_with_a_tenant_admin_floor() -> None:
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == TOOLS_SIGN_OUT_OP_ID)
            )
        ).scalar_one()
    assert (row.product, row.version, row.impl_id) == ("tools", "1.x", "tools-admin")
    assert row.safety_level == "caution"
    assert row.requires_approval is True
    assert row.parameter_schema["required"] == ["user", "tool"]
    assert "web" not in row.parameter_schema["properties"]["tool"]["enum"]
    assert role_floor_for(TOOLS_SIGN_OUT_OP_ID) is TenantRole.TENANT_ADMIN


@pytest.mark.parametrize(
    ("operator", "refused"),
    [
        (_operator(), False),
        (_operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR), True),
        (_operator("svc:a", kind=PrincipalKind.SERVICE), True),
    ],
)
def test_only_a_person_may_ask(operator: Operator, refused: bool) -> None:
    refusal = tools_sign_out_requester_refusal(operator, {})
    assert (refusal == NEEDS_PERSON) is refused


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def test_every_call_waits_and_the_card_names_the_person_by_id() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        call = await call_operation(_operator(), _args("bob"))

    assert call["status"] == "awaiting_approval", call
    (request,) = await _approval_rows()
    preview = request.proposed_effect["preview"]
    assert preview["user"] == {"id": _BOB, "username": "bob"}
    assert preview["tool"] == {"key": "claude-desktop", "client_id": _DESKTOP}
    assert preview["result_if_run"] == "signed_out"
    assert (_BOB, _DESKTOP) in realm.offline


async def test_the_card_of_a_person_of_another_tenant_says_it_fails() -> None:
    with respx.mock(assert_all_called=False) as router:
        _FakeRealm(router)
        call = await call_operation(_operator(), _args("carol"))

    assert call["status"] == "awaiting_approval", call
    (request,) = await _approval_rows()
    preview = request.proposed_effect["preview"]
    assert preview["user"] is None
    assert preview["result_if_run"] == "fails"


@pytest.mark.parametrize(
    "asker",
    [
        _operator("agent:a", kind=PrincipalKind.AGENT, role=TenantRole.OPERATOR),
        _operator("p-op", role=TenantRole.OPERATOR),
    ],
)
async def test_an_agent_and_an_operator_are_refused(asker: Operator) -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        call = await call_operation(asker, _args("bob"))

    assert call["status"] == "denied", call
    assert await _approval_rows() == []
    assert (_BOB, _DESKTOP) in realm.offline


async def _park(user: str = "bob") -> uuid.UUID:
    call = await call_operation(_operator(), _args(user))
    assert call["status"] == "awaiting_approval", call
    return uuid.UUID(str(call["extras"]["approval_request_id"]))


async def test_an_operator_cannot_approve_the_sign_out() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        request_id = await _park()
        approver = _operator("p-op", role=TenantRole.OPERATOR, web_session=True)
        async with get_sessionmaker()() as session:
            with pytest.raises(ApprovalRoleFloorError):
                await approve_request(session, request_id, operator=approver)
            await session.rollback()

    assert (_BOB, _DESKTOP) in realm.offline


async def test_a_tenant_admin_approves_and_only_that_tool_ends() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        request_id = await _park()
        approver = _operator("p-second-admin", web_session=True)
        async with get_sessionmaker()() as session:
            request = await approve_request(session, request_id, operator=approver)
            await session.commit()
        result = await resume_dispatch_after_approval(operator=approver, request=request)

    assert result.status == "ok", result
    assert result.result["user_id"] == _BOB
    assert result.result["outcome"] == "signed_out"
    assert (_BOB, _DESKTOP) not in realm.offline
    assert (_ALICE, _DESKTOP) in realm.offline


async def test_a_person_whose_id_is_not_a_uuid_is_approved_and_signed_out() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        request_id = await _park("dora")
        approver = _operator("p-second-admin", web_session=True)
        async with get_sessionmaker()() as session:
            request = await approve_request(session, request_id, operator=approver)
            await session.commit()
        result = await resume_dispatch_after_approval(operator=approver, request=request)

    assert result.status == "ok", result
    assert result.result["user_id"] == _DORA
    assert result.result["outcome"] == "signed_out"
    assert (_DORA, _DESKTOP) not in realm.offline
    assert (_ALICE, _DESKTOP) in realm.offline


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------


async def test_the_run_ends_one_tool_and_the_audit_row_names_both_people() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        result = await _run("alice")

    assert result.status == "ok", result
    assert result.result == {
        "user_id": _ALICE,
        "username": "alice",
        "tool": "claude-desktop",
        "client_id": _DESKTOP,
        "outcome": "signed_out",
        "offline_login_ended": True,
        "sessions_ended": 0,
        "shared_with": [],
        "audit_id": result.result["audit_id"],
    }
    assert (_ALICE, _DESKTOP) not in realm.offline
    assert (_ALICE, _CODE) in realm.offline
    (row,) = await _sign_out_rows()
    assert str(row.id) == result.result["audit_id"]
    assert row.operator_sub == "p-admin"
    assert row.status_code == 200
    assert row.payload["user_id"] == _ALICE
    assert row.payload["username"] == "alice"
    assert row.payload["client_id"] == _DESKTOP
    assert row.payload["outcome"] == "signed_out"


async def test_the_approved_run_signs_out_the_id_on_the_card() -> None:
    card = {"user": {"id": _ALICE, "username": "alice"}}
    token = approved_preview_var.set((TOOLS_SIGN_OUT_OP_ID, card))
    try:
        with respx.mock(assert_all_called=False) as router:
            realm = _FakeRealm(router)
            # The name now points at another person; the card's id wins.
            result = await _run("bob")
    finally:
        approved_preview_var.reset(token)

    assert result.status == "ok", result
    assert result.result["user_id"] == _ALICE
    assert (_ALICE, _DESKTOP) not in realm.offline
    assert (_BOB, _DESKTOP) in realm.offline


@pytest.mark.parametrize("card", [{}, {"user": None}, {"user": {"username": "bob"}}])
async def test_an_approved_run_with_no_person_on_the_card_is_refused(
    card: dict[str, Any],
) -> None:
    token = approved_preview_var.set((TOOLS_SIGN_OUT_OP_ID, card))
    try:
        with respx.mock(assert_all_called=False) as router:
            realm = _FakeRealm(router)
            result = await _run("bob")
    finally:
        approved_preview_var.reset(token)

    assert result.status == "error", result
    assert NO_CARD_USER in _error_text(result)
    assert (_BOB, _DESKTOP) in realm.offline
    assert await _sign_out_rows() == []


async def test_nothing_to_sign_out_is_ok_and_writes_no_sign_out_row() -> None:
    with respx.mock(assert_all_called=False) as router:
        _FakeRealm(router)
        result = await _run("bob", tool="claude-code")

    assert result.status == "ok", result
    assert result.result["outcome"] == "nothing_to_sign_out"
    assert result.result["audit_id"] is None
    assert await _sign_out_rows() == []


# ---------------------------------------------------------------------------
# Bad cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "user", ["carol", _CAROL, "nobody", "5d0c1a52-0000-4000-8000-00000000dddd"]
)
async def test_a_person_outside_the_tenant_or_unknown_is_not_found(user: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        result = await _run(user)

    assert result.status == "error", result
    assert "in your tenant" in _error_text(result)
    assert (_CAROL, _DESKTOP) in realm.offline
    assert await _sign_out_rows() == []


async def test_the_switch_off_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_CLIENT_PER_TOOL_KIND", "false")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router)
        result = await _run("bob")

    assert result.status == "error", result
    assert "MCP_CLIENT_PER_TOOL_KIND" in _error_text(result)
    assert (_BOB, _DESKTOP) in realm.offline


async def test_the_web_console_cannot_be_signed_out() -> None:
    with respx.mock(assert_all_called=False) as router:
        _FakeRealm(router)
        result = await _run("bob", tool="web")

    assert result.status != "ok", result
    assert await _sign_out_rows() == []


async def test_a_keycloak_failure_is_an_error_and_writes_no_signed_out_row() -> None:
    with respx.mock(assert_all_called=False) as router:
        realm = _FakeRealm(router, fail_sessions=True)
        result = await _run("bob")

    assert result.status == "error", result
    assert "could not sign out" in _error_text(result)
    assert (_BOB, _DESKTOP) in realm.offline
    rows = await _sign_out_rows()
    assert [r.payload["outcome"] for r in rows] == ["failed"]
    assert rows[0].status_code == 502
    assert rows[0].payload["user_id"] == _BOB


async def test_no_keycloak_admin_client_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYCLOAK_ADMIN_URL")
    get_settings.cache_clear()
    result = await _run("bob")

    assert result.status == "error", result
    assert "not set up" in _error_text(result)
    assert await _sign_out_rows() == []
