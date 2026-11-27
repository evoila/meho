# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared parts of the tests of the install import card, per reader.

Two made-up tenants: ``TENANT`` (where the import waits) and
:data:`TENANT_B` (whose stored approver routing the card names).
"""

from __future__ import annotations

import copy
import json
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, TenantPolicy
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session

from ._install_limits_helpers import FILE_P, TENANT, pending_requests, post, token

TENANT_B = uuid.UUID("b0b0b0b0-0000-4000-8000-00000000000b")
B = str(TENANT_B)
B_WARNING = f"Tenant {B}:"

INSTALL_FILE = FILE_P + "routing:\n  - {match: {category: network}, approvers: [Admins]}\n"
_B_ROUTING: dict[str, Any] = {
    "classifications": [],
    "routing": [{"match": {"category": "network"}, "approvers": ["Operators", "Admins"]}],
}

#: The console asks the bell panel and the list as HTMX fragments.
HX_HEADERS = {"HX-Request": "true"}


def reader(
    role: TenantRole,
    *,
    kind: PrincipalKind = PrincipalKind.USER,
    platform_admin: bool = False,
    tenant_id: uuid.UUID = TENANT,
) -> Operator:
    return Operator(
        sub=f"reader-{role.value}-{kind.value}",
        tenant_id=tenant_id,
        tenant_role=role,
        principal_kind=kind,
        platform_admin=platform_admin,
        raw_jwt="header.payload.signature",
        scopes=frozenset({MCP_ADMIN_SCOPE}),
    )


PLATFORM_ADMIN = reader(TenantRole.TENANT_ADMIN, platform_admin=True)


@pytest.fixture(name="switches")
def switches_fixture(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(name="as_reader")
def as_reader_fixture() -> Iterator[Any]:
    """Set who calls REST and MCP for the next requests."""

    def _set(who: Operator) -> None:
        async def _verify() -> Operator:
            return who

        app.dependency_overrides[verify_jwt_and_bind] = _verify
        app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify

    yield _set
    app.dependency_overrides.pop(verify_jwt_and_bind, None)
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def store_tenant_b_routing() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(TenantPolicy(tenant_id=TENANT_B, document=_B_ROUTING, set_by_sub="fixture"))


async def _add_agents_part_for_b(request_id: uuid.UUID) -> None:
    """Put an ``agents_above_limit`` item for tenant B on the stored card, by hand."""
    async with get_sessionmaker()() as session, session.begin():
        row = await session.get(ApprovalRequest, request_id)
        assert row is not None
        effect = copy.deepcopy(row.proposed_effect)
        card = effect["preview"]
        card["diff"]["agents_above_limit"] = [{"tenant_id": B, "rules": ["qzx-rule-41"]}]
        card["warnings"].append(f"{B_WARNING} rule qzx-rule-41 lets an agent go above the limit.")
        row.proposed_effect = effect


async def ask(client: TestClient, key: Any, *, tenant_data: bool = True) -> uuid.UUID:
    """A platform admin asks for the install import by token; return the request id.

    With *tenant_data* the card names tenant B in ``routing_floor`` and in
    ``agents_above_limit``. Without it, the card's ``routing_floor`` is an
    empty list.
    """
    if tenant_data:
        await store_tenant_b_routing()
    asked = post(client, "import", token(key, sub="admin-a"), INSTALL_FILE, scope="install")
    assert asked.status_code == 202, asked.text
    (request,) = await pending_requests()
    card = request.proposed_effect["preview"]
    if not tenant_data:
        assert card["diff"]["routing_floor"] == []
        assert not any(w.startswith(B_WARNING) for w in card["warnings"])
        return request.id
    assert [item["tenant_id"] for item in card["diff"]["routing_floor"]] == [B]
    assert any(w.startswith(B_WARNING) for w in card["warnings"])
    await _add_agents_part_for_b(request.id)
    return request.id


def shaped(card: dict[str, Any]) -> dict[str, Any]:
    """The card a reader who is not a platform admin gets: same keys, no tenant B data."""
    shown = copy.deepcopy(card)
    for part in ("routing_floor", "agents_above_limit"):
        if part in shown["diff"]:
            shown["diff"][part] = []
    shown["warnings"] = [w for w in shown["warnings"] if not w.startswith(B_WARNING)]
    return shown


def names_b(value: Any) -> bool:
    return B in (value if isinstance(value, str) else json.dumps(value))


def rest_cards(client: TestClient, request_id: uuid.UUID) -> tuple[Any, Any]:
    """The REST get and list answers for *request_id*: (status, card) each."""
    headers = {"Authorization": "Bearer stub"}
    got = client.get(f"/api/v1/approvals/{request_id}", headers=headers)
    listed = client.get("/api/v1/approvals", params={"status": "pending"}, headers=headers)
    got_card = got.json()["proposed_effect"]["preview"] if got.status_code == 200 else None
    list_cards = (
        [i["proposed_effect"]["preview"] for i in listed.json() if i["id"] == str(request_id)]
        if listed.status_code == 200
        else None
    )
    return (got.status_code, got_card), (listed.status_code, list_cards)


def _mcp(client: TestClient, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }
    answer = client.post("/mcp", json=body).json()
    if "error" in answer:
        return {"error": answer["error"]}
    result = answer["result"]
    if result.get("isError"):
        return {"error": result}
    structured = result.get("structuredContent")
    return structured if structured is not None else json.loads(result["content"][0]["text"])


def mcp_cards(client: TestClient, request_id: uuid.UUID) -> tuple[Any, Any, Any, Any]:
    got = _mcp(client, "meho_approvals_get", {"approval_request_id": str(request_id)})
    listed = _mcp(client, "meho_approvals_list", {"status": "pending"})
    got_card = None if "error" in got else got["proposed_effect"]["preview"]
    list_cards = (
        None
        if "error" in listed
        else [
            i["proposed_effect"]["preview"] for i in listed["items"] if i["id"] == str(request_id)
        ]
    )
    return got, got_card, listed, list_cards


async def console_session(
    client: TestClient, key: Any, *, sub: str, role: TenantRole, platform_admin: bool
) -> None:
    signed_in = token(
        key,
        sub=sub,
        platform_admin=platform_admin,
        tenant_role=role.value,
        extra_claims={
            "auth_time": int(time.time()),
            "acr": "1",
            "sid": str(uuid.uuid4()),
            "azp": "meho-web",
        },
    )
    async with get_sessionmaker()() as session, session.begin():
        web = await create_session(
            session,
            operator_sub=sub,
            tenant_id=TENANT,
            access_token=signed_in,
            refresh_token="refresh-token",
            lifetime=timedelta(hours=1),
        )
    client.cookies.set(SESSION_COOKIE_NAME, str(web.id))


def console_pages(client: TestClient, request_id: uuid.UUID) -> dict[str, Any]:
    """The detail page, the bell panel and the list, asked as the console asks them."""
    return {
        "detail": client.get(f"/ui/approvals/{request_id}"),
        "panel": client.get("/ui/approvals", headers=HX_HEADERS),
        "list": client.get("/ui/approvals/list", params={"tab": "pending"}, headers=HX_HEADERS),
    }
