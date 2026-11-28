# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The install import card names another tenant's agent rules only to a platform admin.

A platform admin asks with a token for an install import that lowers the
outer limit. The card builder lists each tenant with a stored personal
agent rule above the new limit (``diff.agents_above_limit``) with one
warning each. A platform admin who reads as a person reads the card as
stored. Every other reader gets the same keys without the other tenant's
data, in every view: REST get and list, ``meho_approvals_get`` and
``meho_approvals_list``, the console detail page and the bell panel. The
stored card stays whole, and the click runs the file. With
``CUSTOM_POLICY`` off, no reader sees the part.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import TenantPolicy
from meho_backplane.mcp.tools.approvals import _effect_for
from meho_backplane.operations.platform_admin_card import card_for_reader
from meho_backplane.settings import get_settings

from ._install_card_readers_helpers import (
    PLATFORM_ADMIN,
    as_reader_fixture,  # noqa: F401
    console_pages,
    console_session,
    mcp_cards,
    reader,
    rest_cards,
    switches_fixture,  # noqa: F401
)
from ._install_limits_helpers import (
    FILE_P,
    client_fixture,  # noqa: F401
    console_approve,
    embedding_fixture,  # noqa: F401
    post,
    requests,
    setup_ops,
    store_install,
    stored_install,
    token,
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_B = uuid.UUID("7c0ffee0-0000-4000-8000-00000000b0b0")
_RULE_NAME = "qzx-made-up-rule-41"
_B_WARNING = f"Tenant {_B}:"
_LOWER = FILE_P + "limits: {personal_agent_up_to: caution}\n"

_SHAPED_READERS = [
    pytest.param(reader(TenantRole.OPERATOR), id="operator"),
    pytest.param(reader(TenantRole.TENANT_ADMIN), id="tenant-admin"),
    pytest.param(reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT), id="agent"),
    pytest.param(reader(TenantRole.OPERATOR, kind=PrincipalKind.SERVICE), id="service"),
    pytest.param(
        reader(TenantRole.OPERATOR, kind=PrincipalKind.AGENT, platform_admin=True),
        id="agent-with-platform-admin",
    ),
]


def _names_b(value: Any) -> bool:
    text = value if isinstance(value, str) else json.dumps(value)
    return str(_B) in text or _RULE_NAME in text


async def _ask(client: TestClient, key: Any) -> tuple[uuid.UUID, dict[str, Any]]:
    """Store tenant B's rule, then ask for the install import; the request id and stored card."""
    await store_install({"classifications": [], "limits": {"personal_agent_up_to": "dangerous"}})
    rule = {
        "name": _RULE_NAME,
        "caller": "personal_agent",
        "roles": ["Admin"],
        "up_to": "dangerous",
    }
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            TenantPolicy(
                tenant_id=_B, document={"classifications": [], "agents": [rule]}, set_by_sub="fx"
            )
        )
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = post(client, "import", token(key, sub="admin-a"), _LOWER, scope="install")
    assert asked.status_code == 202, asked.text
    (row,) = await requests()
    card: dict[str, Any] = row.proposed_effect["preview"]
    (item,) = card["diff"]["agents_above_limit"]
    assert item["tenant_id"] == str(_B)
    assert _RULE_NAME in json.dumps(item)
    (warning,) = [w for w in card["warnings"] if w.startswith(_B_WARNING)]
    assert f"(`{_RULE_NAME}`) allows dangerous, above the new limit caution." in warning
    return row.id, card


def _shaped(card: dict[str, Any]) -> dict[str, Any]:
    diff = {**card["diff"], "agents_above_limit": [], "routing_floor": []}
    if "routing_floor" not in card["diff"]:
        del diff["routing_floor"]
    warnings = [w for w in card["warnings"] if not w.startswith(_B_WARNING)]
    return {**card, "diff": diff, "warnings": warnings}


def _views(client: TestClient, request_id: uuid.UUID) -> tuple[list[Any], list[Any]]:
    """The cards of REST get and list and of the two MCP tools, and the whole answers."""
    (got_status, got), (list_status, listed) = rest_cards(client, request_id)
    mcp_got, mcp_card, mcp_listed, mcp_list = mcp_cards(client, request_id)
    assert (got_status, list_status) == (200, 200)
    cards = [got, *(listed or []), mcp_card, *(mcp_list or [])]
    return cards, [mcp_got, mcp_listed]


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_the_platform_admin_reads_the_card_as_stored_and_the_click_runs_the_file(
    client: TestClient, embedding: AsyncMock, as_reader: Any
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-admin")
    request_id, stored = await _ask(client, key)
    as_reader(PLATFORM_ADMIN)
    cards, _answers = _views(client, request_id)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await console_session(
            client, key, sub="admin-b", role=TenantRole.TENANT_ADMIN, platform_admin=True
        )
        pages = console_pages(client, request_id)
        client.cookies.clear()
        clicked = await console_approve(client, key, request_id, sub="admin-b")

    assert len(cards) == 4
    for card in cards:
        assert card == stored
    assert _names_b(pages["detail"].text)
    assert clicked.status_code == 200, clicked.text
    (row,) = await requests()
    assert row.proposed_effect["preview"] == stored
    install = await stored_install()
    assert install is not None
    assert install["limits"] == {"personal_agent_up_to": "caution"}


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize("who", _SHAPED_READERS)
async def test_every_other_reader_gets_the_keys_without_the_other_tenant(
    client: TestClient, embedding: AsyncMock, as_reader: Any, who: Operator
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-shaped")
    request_id, stored = await _ask(client, key)
    as_reader(who)
    cards, answers = _views(client, request_id)

    assert len(cards) == 4
    for card in cards:
        assert card == _shaped(stored)
        assert card.keys() == stored.keys()
        assert card["diff"].keys() == stored["diff"].keys()
        assert card["diff"]["agents_above_limit"] == []
        assert not any(w.startswith(_B_WARNING) for w in card["warnings"])
    for answer in [*cards, *answers]:
        assert not _names_b(answer)
    (row,) = await requests()
    assert row.proposed_effect["preview"] == stored


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_a_reader_is_refused_and_a_tenant_b_admin_finds_nothing(
    client: TestClient, embedding: AsyncMock, as_reader: Any
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-refused")
    request_id, _stored = await _ask(client, key)
    as_reader(reader(TenantRole.READ_ONLY))
    (got_status, _g), (list_status, _l) = rest_cards(client, request_id)
    mcp_got, _c, mcp_listed, _m = mcp_cards(client, request_id)
    as_reader(reader(TenantRole.TENANT_ADMIN, tenant_id=_B))
    (b_status, _bg), (b_list_status, b_list) = rest_cards(client, request_id)

    assert (got_status, list_status) == (403, 403)
    assert "error" in mcp_got and "error" in mcp_listed
    assert not _names_b(mcp_got) and not _names_b(mcp_listed)
    assert b_status == 404
    assert (b_list_status, b_list) == (200, [])


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize(
    "role", [TenantRole.OPERATOR, TenantRole.READ_ONLY, TenantRole.TENANT_ADMIN]
)
async def test_the_console_and_the_bell_panel_show_no_one_else_the_other_tenant(
    client: TestClient, embedding: AsyncMock, role: TenantRole
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-console")
    request_id, _stored = await _ask(client, key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await console_session(client, key, sub="person-a", role=role, platform_admin=False)
        pages = console_pages(client, request_id)
    client.cookies.clear()

    for name, page in pages.items():
        assert page.status_code == 200, (name, page.text)
        assert POLICY_APPLY_OP_ID in page.text, name
        assert not _names_b(page.text), name


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_a_platform_admin_under_an_acting_agent_gets_the_shaped_card(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-actor")
    _request_id, stored = await _ask(client, key)
    (row,) = await requests()
    with actor_delegation("agent-of-admin"):
        rest = card_for_reader(row.op_id, row.proposed_effect, reader=PLATFORM_ADMIN, whole=True)
        mcp = _effect_for(row, PLATFORM_ADMIN)

    assert mcp is not None
    for effect in (rest, mcp):
        assert effect["preview"] == _shaped(stored)
        assert not _names_b(effect)


@pytest.mark.asyncio
async def test_with_the_switch_off_no_reader_sees_the_part(
    client: TestClient,
    embedding: AsyncMock,
    as_reader: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("CUSTOM_POLICY", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    get_settings.cache_clear()
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-agents-card-off")
    request_id, stored = await _ask(client, key)
    monkeypatch.delenv("CUSTOM_POLICY")
    get_settings.cache_clear()

    for who in (PLATFORM_ADMIN, reader(TenantRole.OPERATOR)):
        as_reader(who)
        cards, answers = _views(client, request_id)
        for card in cards:
            assert "agents_above_limit" not in card["diff"]
            assert "planned_with_custom_policy" not in card
            assert not any(w.startswith(_B_WARNING) for w in card["warnings"])
        for answer in [*cards, *answers]:
            assert _RULE_NAME not in json.dumps(answer)
    (row,) = await requests()
    assert row.proposed_effect["preview"] == stored
    get_settings.cache_clear()
