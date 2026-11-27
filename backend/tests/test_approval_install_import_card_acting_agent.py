# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The install import card, read in a call that runs with an acting agent.

Only a platform admin's own call as a person reads the tenant data of the
card. A call that runs with an acting agent (for example a hosted agent
run a platform admin started) gets the card as every reader who is not a
platform admin gets it: in the REST view and in both MCP approvals tools.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.api.v1.approvals import _view
from meho_backplane.auth.delegation import actor_delegation, delegated_operator
from meho_backplane.auth.operator import Operator
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler

from ._install_card_readers_helpers import (
    PLATFORM_ADMIN,
    ask,
    shaped,
    switches_fixture,  # noqa: F401
)
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
    requests,
    setup_ops,
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_AGENT = "agent-made-up-7"


async def _cards(who: Operator) -> list[dict[str, Any]]:
    """The card of the one stored request in the REST view and both MCP tools, for *who*."""
    (row,) = await requests()
    rest = _view(row, operator=who).proposed_effect["preview"]
    got = await _get_handler(who, {"approval_request_id": str(row.id)})
    listed = await _list_handler(who, {"status": "pending"})
    (item,) = listed["items"]
    return [rest, got["proposed_effect"]["preview"], item["proposed_effect"]["preview"]]


async def _stored_card(client: TestClient) -> dict[str, Any]:
    key = make_rsa_keypair("kid-card-acting-agent")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await ask(client, key)
    (row,) = await requests()
    card: dict[str, Any] = row.proposed_effect["preview"]
    assert card != shaped(card)
    return card


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_a_platform_admin_under_an_acting_agent_gets_the_shaped_card(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    stored = await _stored_card(client)
    with actor_delegation(_AGENT):
        cards = await _cards(PLATFORM_ADMIN)
    assert cards == [shaped(stored)] * 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_the_same_platform_admin_in_a_call_of_their_own_reads_the_card_as_stored(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    stored = await _stored_card(client)
    assert await _cards(PLATFORM_ADMIN) == [stored] * 3


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_a_hosted_agent_run_started_by_a_platform_admin_gets_the_shaped_card(
    client: TestClient, embedding: AsyncMock
) -> None:
    await setup_ops(embedding)
    stored = await _stored_card(client)
    # A hosted run binds the agent as actor and calls its tools with the
    # person narrowed to the agent's limits (``AgentInvoker.run``).
    with actor_delegation(_AGENT):
        cards = await _cards(delegated_operator(PLATFORM_ADMIN))
    assert cards == [shaped(stored)] * 3
