# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of an install policy import, as each reader reads it.

The card of an install import names the tenants whose stored approver
routing is wider than the new file (``diff.routing_floor`` and a warning
per entry). A platform admin who is a person reads those parts. Every other
reader gets the card without them, in every view: REST get and list, the
MCP tools ``meho_approvals_get`` and ``meho_approvals_list`` (through
``tools/call``, with the tool's role check), and the console detail page,
list and panel. The stored card stays whole, and the run after a click in
the console applies the full file.

Two made-up tenants: ``TENANT`` (where the import waits) and
:data:`_TENANT_B` (whose stored routing the card names).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.connectors.policy.apply_card_readers import card_without_tenant_parts
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EventOutbox, TenantPolicy
from meho_backplane.main import app
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.middleware import verify_jwt_and_bind
from meho_backplane.operations.platform_admin_card import card_for_reader
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth import SESSION_COOKIE_NAME
from meho_backplane.ui.auth.session_store import create_session

from ._install_limits_helpers import (
    FILE_P,
    TENANT,
    client_fixture,  # noqa: F401
    console_approve,
    embedding_fixture,  # noqa: F401
    pending_requests,
    post,
    requests,
    setup_ops,
    stored_install,
    token,
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_TENANT_B = uuid.UUID("b0b0b0b0-0000-4000-8000-00000000000b")
_B = str(_TENANT_B)

_INSTALL_FILE = FILE_P + "routing:\n  - {match: {category: network}, approvers: [Admins]}\n"
_B_ROUTING: dict[str, Any] = {
    "classifications": [],
    "routing": [{"match": {"category": "network"}, "approvers": ["Operators", "Admins"]}],
}


def _reader(
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


#: Each reader who is not a platform admin, with what REST and MCP answer
#: for the request: ``shown`` (the card without the tenant parts),
#: ``refused`` (no right to read approvals) or ``not_found`` (another tenant).
_OTHER_READERS = [
    pytest.param(_reader(TenantRole.OPERATOR), "shown", id="operator"),
    pytest.param(_reader(TenantRole.READ_ONLY), "refused", id="reader"),
    pytest.param(_reader(TenantRole.TENANT_ADMIN), "shown", id="tenant-admin"),
    pytest.param(_reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT), "shown", id="agent"),
    pytest.param(_reader(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE), "shown", id="service"),
    pytest.param(
        _reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, platform_admin=True),
        "shown",
        id="agent-with-platform-admin",
    ),
    pytest.param(
        _reader(TenantRole.TENANT_ADMIN, tenant_id=_TENANT_B), "not_found", id="tenant-b-admin"
    ),
]

_PLATFORM_ADMIN = _reader(TenantRole.TENANT_ADMIN, platform_admin=True)


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

    def _set(reader: Operator) -> None:
        async def _verify() -> Operator:
            return reader

        app.dependency_overrides[verify_jwt_and_bind] = _verify
        app.dependency_overrides[verify_mcp_jwt_and_bind] = _verify

    yield _set
    app.dependency_overrides.pop(verify_jwt_and_bind, None)
    app.dependency_overrides.pop(verify_mcp_jwt_and_bind, None)


async def _store_tenant_b_routing() -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(TenantPolicy(tenant_id=_TENANT_B, document=_B_ROUTING, set_by_sub="fixture"))


async def _ask(client: TestClient, key: Any) -> uuid.UUID:
    """A platform admin asks for the install import by token; return the request id."""
    await _store_tenant_b_routing()
    asked = post(client, "import", token(key, sub="admin-a"), _INSTALL_FILE, scope="install")
    assert asked.status_code == 202, asked.text
    (request,) = await pending_requests()
    card = request.proposed_effect["preview"]
    assert [item["tenant_id"] for item in card["diff"]["routing_floor"]] == [_B]
    assert any(w.startswith(f"Tenant {_B}:") for w in card["warnings"])
    return request.id


def _card_names_b(card: dict[str, Any]) -> bool:
    return _B in json.dumps(card)


def _rest_cards(client: TestClient, request_id: uuid.UUID) -> tuple[Any, Any]:
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


def _mcp_cards(client: TestClient, request_id: uuid.UUID) -> tuple[Any, Any]:
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


async def _console_session(
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


def _console_pages(client: TestClient, request_id: uuid.UUID) -> dict[str, Any]:
    return {
        "detail": client.get(f"/ui/approvals/{request_id}"),
        "list": client.get("/ui/approvals/list", params={"tab": "pending"}),
        "panel": client.get("/ui/approvals"),
    }


# ---------------------------------------------------------------------------
# REST and MCP, per reader
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize(("reader", "answer"), _OTHER_READERS)
async def test_a_reader_who_is_not_a_platform_admin_gets_the_card_without_the_tenant_parts(
    client: TestClient,
    embedding: AsyncMock,
    as_reader: Any,
    reader: Operator,
    answer: str,
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-readers")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await _ask(client, key)
    as_reader(reader)

    (get_status, rest_card), (list_status, rest_list) = _rest_cards(client, request_id)
    mcp_got, mcp_card, mcp_listed, mcp_list = _mcp_cards(client, request_id)

    if answer == "refused":
        assert (get_status, list_status) == (403, 403)
        assert "error" in mcp_got and "error" in mcp_listed
        assert not _card_names_b(mcp_got) and not _card_names_b(mcp_listed)
    elif answer == "not_found":
        assert get_status == 404
        assert (list_status, rest_list) == (200, [])
        assert "error" in mcp_got
        assert mcp_list == []
    else:
        assert (get_status, list_status) == (200, 200)
        assert rest_list is not None and len(rest_list) == 1
        assert mcp_list is not None and len(mcp_list) == 1
        for card in (rest_card, rest_list[0], mcp_card, mcp_list[0]):
            assert "routing_floor" not in card["diff"]
            assert not _card_names_b(card)
            assert card["scope"] == "install"

    # The stored card stays whole.
    (stored,) = await requests()
    assert [i["tenant_id"] for i in stored.proposed_effect["preview"]["diff"]["routing_floor"]] == [
        _B
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_a_platform_admin_reads_the_card_as_stored_in_rest_and_mcp(
    client: TestClient, embedding: AsyncMock, as_reader: Any
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-admin")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await _ask(client, key)
    (stored,) = await requests()
    as_reader(_PLATFORM_ADMIN)

    (get_status, rest_card), (list_status, rest_list) = _rest_cards(client, request_id)
    _got, mcp_card, _listed, mcp_list = _mcp_cards(client, request_id)

    assert (get_status, list_status) == (200, 200)
    for card in (rest_card, rest_list[0], mcp_card, mcp_list[0]):
        assert (
            card["diff"]["routing_floor"]
            == stored.proposed_effect["preview"]["diff"]["routing_floor"]
        )
        assert card["warnings"] == stored.proposed_effect["preview"]["warnings"]


# ---------------------------------------------------------------------------
# The console, and the run after the click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize(
    "role", [TenantRole.OPERATOR, TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN]
)
async def test_the_console_shows_the_tenant_parts_to_no_one_but_a_platform_admin(
    client: TestClient, embedding: AsyncMock, role: TenantRole
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-console")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await _ask(client, key)
        await _console_session(client, key, sub="person-a", role=role, platform_admin=False)
        pages = _console_pages(client, request_id)
    client.cookies.clear()

    assert pages["detail"].status_code == 200, pages["detail"].text
    assert POLICY_APPLY_OP_ID in pages["detail"].text
    for name, page in pages.items():
        assert _B not in page.text, name


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_the_platform_admin_who_approves_sees_every_part_and_the_run_applies_the_file(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-approve")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await _ask(client, key)
        (stored_before,) = await requests()
        await _console_session(
            client, key, sub="admin-b", role=TenantRole.TENANT_ADMIN, platform_admin=True
        )
        detail = client.get(f"/ui/approvals/{request_id}")
        client.cookies.clear()
        clicked = await console_approve(client, key, request_id, sub="admin-b")

    assert detail.status_code == 200, detail.text
    assert _B in detail.text
    assert clicked.status_code == 200, clicked.text
    assert await pending_requests() == []
    (stored_after,) = await requests()
    assert stored_after.proposed_effect == stored_before.proposed_effect
    install = await stored_install()
    assert install is not None
    assert install["routing"] == [{"match": {"category": "network"}, "approvers": ["Admins"]}]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_no_audit_row_and_no_broadcast_event_names_the_other_tenant(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-audit")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await _ask(client, key)
    async with get_sessionmaker()() as session:
        audit = list((await session.execute(select(AuditLog))).scalars())
        events = list((await session.execute(select(EventOutbox))).scalars())
    assert audit
    for row in [*audit, *events]:
        text = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
        assert _B not in text


# ---------------------------------------------------------------------------
# With the routing switches off, every card is as stored
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_routing_off_every_reader_gets_the_card_as_stored(
    client: TestClient,
    embedding: AsyncMock,
    as_reader: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    monkeypatch.delenv("POLICY_APPROVER_ROUTING", raising=False)
    get_settings.cache_clear()
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await _ask_without_floor(client, key)
    (stored,) = await requests()
    as_reader(_reader(TenantRole.OPERATOR))

    (_s, rest_card), (_l, rest_list) = _rest_cards(client, request_id)
    _got, mcp_card, _listed, mcp_list = _mcp_cards(client, request_id)
    for card in (rest_card, rest_list[0], mcp_card, mcp_list[0]):
        assert card == stored.proposed_effect["preview"]
    get_settings.cache_clear()


async def _ask_without_floor(client: TestClient, key: Any) -> uuid.UUID:
    await _store_tenant_b_routing()
    asked = post(client, "import", token(key, sub="admin-a"), _INSTALL_FILE, scope="install")
    assert asked.status_code == 202, asked.text
    (request,) = await pending_requests()
    assert "routing_floor" not in request.proposed_effect["preview"]["diff"]
    return request.id


# ---------------------------------------------------------------------------
# The shared function
# ---------------------------------------------------------------------------

_CARD: dict[str, Any] = {
    "scope": "install",
    "warnings": [f"Tenant {_B}: wider.", "Another warning."],
    "diff": {
        "routing_floor": [{"tenant_id": _B, "entries": ["wider."]}],
        "agents_above_limit": [{"tenant_id": "c0c0c0c0", "rules": []}],
        "routing": {"added": []},
    },
}


def test_the_filter_leaves_out_every_tenant_part_and_its_warnings() -> None:
    shown = card_without_tenant_parts(_CARD)
    assert shown == {
        "scope": "install",
        "warnings": ["Another warning."],
        "diff": {"routing": {"added": []}},
    }
    assert "routing_floor" in _CARD["diff"]


@pytest.mark.parametrize(
    "card",
    [
        pytest.param({**_CARD, "scope": "tenant"}, id="tenant-scope"),
        pytest.param({"scope": "install", "warnings": ["x"], "diff": {}}, id="no-tenant-part"),
        pytest.param({"scope": "install", "diff": None}, id="no-diff"),
    ],
)
def test_the_filter_leaves_every_other_card_as_it_is(card: dict[str, Any]) -> None:
    assert card_without_tenant_parts(card) is card


def test_card_for_reader_shapes_only_the_cards_of_an_op_with_a_filter() -> None:
    effect = {"safety_level": "dangerous", "preview": _CARD}
    assert card_for_reader("other.op", effect, reader=None) is effect
    assert card_for_reader(POLICY_APPLY_OP_ID, effect, reader=_PLATFORM_ADMIN) is effect
    for reader in (None, _reader(TenantRole.TENANT_ADMIN)):
        shown = card_for_reader(POLICY_APPLY_OP_ID, effect, reader=reader)
        assert shown["safety_level"] == "dangerous"
        assert "routing_floor" not in shown["preview"]["diff"]
