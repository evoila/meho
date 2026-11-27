# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of an install policy import, as each reader reads it.

The card of an install import names the tenants whose stored policy the
new file affects (``diff.routing_floor``, ``diff.agents_above_limit`` and a
warning per entry). A platform admin who is a person reads the card as
stored. Every other reader gets the same keys, without the tenant data
inside them, in every view: REST get and list, the MCP tools
``meho_approvals_get`` and ``meho_approvals_list`` (through ``tools/call``,
with the tool's role check), and the console detail page, bell panel and
list. A card with no tenant data reads as stored for every reader. The
stored card stays whole, and the run after a click in the console applies
the full file.
"""

from __future__ import annotations

import json
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
from meho_backplane.db.models import AuditLog, EventOutbox
from meho_backplane.operations.platform_admin_card import card_for_reader
from meho_backplane.settings import get_settings

from ._install_card_readers_helpers import (
    INSTALL_FILE,
    PLATFORM_ADMIN,
    TENANT_B,
    B,
    as_reader_fixture,  # noqa: F401
    ask,
    console_pages,
    console_session,
    mcp_cards,
    names_b,
    reader,
    rest_cards,
    shaped,
    store_tenant_b_routing,
    switches_fixture,  # noqa: F401
)
from ._install_limits_helpers import (
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

#: Each reader of the install import, with what REST and MCP answer:
#: ``as_stored`` (the card as stored), ``shaped`` (the same keys without
#: tenant B's data), ``refused`` (no right to read approvals) or
#: ``not_found`` (another tenant).
_READERS = [
    pytest.param(PLATFORM_ADMIN, "as_stored", id="platform-admin"),
    pytest.param(reader(TenantRole.OPERATOR), "shaped", id="operator"),
    pytest.param(reader(TenantRole.READ_ONLY), "refused", id="reader"),
    pytest.param(reader(TenantRole.TENANT_ADMIN), "shaped", id="tenant-admin"),
    pytest.param(reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT), "shaped", id="agent"),
    pytest.param(reader(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE), "shaped", id="service"),
    pytest.param(reader(TenantRole.OPERATOR, kind=PrincipalKind.RUNNER), "shaped", id="runner"),
    pytest.param(
        reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, platform_admin=True),
        "shaped",
        id="agent-with-platform-admin",
    ),
    pytest.param(
        reader(TenantRole.TENANT_ADMIN, tenant_id=TENANT_B), "not_found", id="tenant-b-admin"
    ),
]


async def _ask_as(client: TestClient, kid: str, *, tenant_data: bool) -> Any:
    key = make_rsa_keypair(kid)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id = await ask(client, key, tenant_data=tenant_data)
    (stored,) = await requests()
    return key, request_id, stored.proposed_effect["preview"]


# ---------------------------------------------------------------------------
# REST and MCP, per reader, for a card with and without tenant data
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize("tenant_data", [True, False], ids=["tenant-data", "empty-tenant-parts"])
@pytest.mark.parametrize(("who", "answer"), _READERS)
async def test_each_reader_gets_the_keys_of_the_stored_card_in_rest_and_mcp(
    client: TestClient,
    embedding: AsyncMock,
    as_reader: Any,
    who: Operator,
    answer: str,
    tenant_data: bool,
) -> None:
    await setup_ops(embedding)
    _key, request_id, stored = await _ask_as(client, "kid-card-readers", tenant_data=tenant_data)
    as_reader(who)

    (get_status, rest_card), (list_status, rest_list) = rest_cards(client, request_id)
    mcp_got, mcp_card, mcp_listed, mcp_list = mcp_cards(client, request_id)

    if answer == "refused":
        assert (get_status, list_status) == (403, 403)
        assert "error" in mcp_got and "error" in mcp_listed
        assert not names_b(mcp_got) and not names_b(mcp_listed)
    elif answer == "not_found":
        assert get_status == 404
        assert (list_status, rest_list) == (200, [])
        assert "error" in mcp_got
        assert mcp_list == []
    else:
        assert (get_status, list_status) == (200, 200)
        assert rest_list is not None and len(rest_list) == 1
        assert mcp_list is not None and len(mcp_list) == 1
        expected = stored if answer == "as_stored" or not tenant_data else shaped(stored)
        for card in (rest_card, rest_list[0], mcp_card, mcp_list[0]):
            assert card == expected
            assert card.keys() == stored.keys()
            assert card["diff"].keys() == stored["diff"].keys()
        if answer == "shaped" and tenant_data:
            assert expected["diff"]["routing_floor"] == []
            assert expected["diff"]["agents_above_limit"] == []
            assert not names_b(expected)

    # The stored card stays whole.
    (row,) = await requests()
    assert row.proposed_effect["preview"] == stored


# ---------------------------------------------------------------------------
# The console, and the run after the click
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize("tenant_data", [True, False], ids=["tenant-data", "empty-tenant-parts"])
@pytest.mark.parametrize(
    "role", [TenantRole.OPERATOR, TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN]
)
async def test_the_console_shows_the_tenant_data_to_no_one_but_a_platform_admin(
    client: TestClient, embedding: AsyncMock, role: TenantRole, tenant_data: bool
) -> None:
    await setup_ops(embedding)
    key, request_id, _stored = await _ask_as(client, "kid-card-console", tenant_data=tenant_data)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await console_session(client, key, sub="person-a", role=role, platform_admin=False)
        pages = console_pages(client, request_id)
    client.cookies.clear()

    for name, page in pages.items():
        assert page.status_code == 200, (name, page.text)
        assert POLICY_APPLY_OP_ID in page.text, name
        assert not names_b(page.text), name


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_the_platform_admin_who_approves_sees_every_part_and_the_run_applies_the_file(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    key, request_id, _stored = await _ask_as(client, "kid-card-approve", tenant_data=True)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        (stored_before,) = await requests()
        await console_session(
            client, key, sub="admin-b", role=TenantRole.TENANT_ADMIN, platform_admin=True
        )
        pages = console_pages(client, request_id)
        client.cookies.clear()
        clicked = await console_approve(client, key, request_id, sub="admin-b")

    for name, page in pages.items():
        assert page.status_code == 200, (name, page.text)
        assert POLICY_APPLY_OP_ID in page.text, name
    assert names_b(pages["detail"].text)
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
    await _ask_as(client, "kid-card-audit", tenant_data=True)
    async with get_sessionmaker()() as session:
        audit = list((await session.execute(select(AuditLog))).scalars())
        events = list((await session.execute(select(EventOutbox))).scalars())
    assert audit
    for row in [*audit, *events]:
        text = json.dumps({c.name: str(getattr(row, c.name)) for c in row.__table__.columns})
        assert B not in text


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
        await store_tenant_b_routing()
        asked = post(client, "import", token(key, sub="admin-a"), INSTALL_FILE, scope="install")
    assert asked.status_code == 202, asked.text
    (stored,) = await requests()
    assert "routing_floor" not in stored.proposed_effect["preview"]["diff"]
    as_reader(reader(TenantRole.OPERATOR))

    (_s, rest_card), (_l, rest_list) = rest_cards(client, stored.id)
    _got, mcp_card, _listed, mcp_list = mcp_cards(client, stored.id)
    for card in (rest_card, rest_list[0], mcp_card, mcp_list[0]):
        assert card == stored.proposed_effect["preview"]
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# The shared function
# ---------------------------------------------------------------------------

_CARD: dict[str, Any] = {
    "scope": "install",
    "warnings": [f"Tenant {B}: wider.", "Tenant c0c0c0c0: above.", "Another warning."],
    "diff": {
        "routing_floor": [{"tenant_id": B, "entries": ["wider."]}],
        "agents_above_limit": [{"tenant_id": "c0c0c0c0", "rules": []}],
        "routing": {"added": []},
    },
}


def test_the_filter_keeps_every_key_and_takes_out_the_tenant_data() -> None:
    shown = card_without_tenant_parts(_CARD)
    assert shown == {
        "scope": "install",
        "warnings": ["Another warning."],
        "diff": {"routing_floor": [], "agents_above_limit": [], "routing": {"added": []}},
    }
    assert _CARD["diff"]["routing_floor"] == [{"tenant_id": B, "entries": ["wider."]}]


@pytest.mark.parametrize(
    "card",
    [
        pytest.param({**_CARD, "scope": "tenant"}, id="tenant-scope"),
        pytest.param({"scope": "install", "warnings": ["x"], "diff": {}}, id="no-tenant-part"),
        pytest.param(
            {
                "scope": "install",
                "warnings": ["x"],
                "diff": {"routing_floor": [], "agents_above_limit": []},
            },
            id="empty-tenant-parts",
        ),
        pytest.param({"scope": "install", "diff": None}, id="no-diff"),
    ],
)
def test_the_filter_leaves_every_other_card_as_it_is(card: dict[str, Any]) -> None:
    assert card_without_tenant_parts(card) is card


def test_card_for_reader_shapes_only_the_cards_of_an_op_with_a_filter() -> None:
    effect = {"safety_level": "dangerous", "preview": _CARD}
    assert card_for_reader("other.op", effect, reader=None) is effect
    assert card_for_reader(POLICY_APPLY_OP_ID, effect, reader=PLATFORM_ADMIN) is effect
    for who in (None, reader(TenantRole.TENANT_ADMIN)):
        shown = card_for_reader(POLICY_APPLY_OP_ID, effect, reader=who)
        assert shown["safety_level"] == "dangerous"
        assert shown["preview"]["diff"]["routing_floor"] == []
