# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""No view shows the install value recorded on a tenant import's card.

With ``CUSTOM_POLICY`` on, the stored card of a tenant import records the
hash of the install's ``limits`` and ``classifications``
(``planned_with_install_limits``). Only the run reads it, from the stored
card. No reader gets it in any view: the REST approvals answers (get and
list), ``meho_approvals_get``, ``meho_approvals_list``,
``preview_operation``, the console detail page and the bell panel. The
readers: a platform admin who reads as a person, a tenant admin, a service
login and an agent login. After the reads, the click runs the file, because
the install's policy is the same.

A tenant admin asks for the import with a token.
"""

from __future__ import annotations

import copy
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_card_readers_helpers import (
    as_reader_fixture,  # noqa: F401
    console_pages,
    console_session,
    mcp_cards,
    reader,
    rest_cards,
    switches_fixture,  # noqa: F401
)
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEY = "planned_with_install_limits"

_INSTALL: dict[str, Any] = {
    "classifications": [{"match": {"op": "inslim.vm.restart"}, "always_wait": True}],
    "limits": {"personal_agent_up_to": "dangerous"},
}

_FILE_RULE = (
    "policy: v1\nclassifications: []\nagents:\n  - name: helper\n"
    "    caller: personal_agent\n    roles: [Admin, Operator]\n    up_to: caution\n"
)

_READERS = [
    pytest.param(reader(TenantRole.TENANT_ADMIN, platform_admin=True), id="platform-admin"),
    pytest.param(reader(TenantRole.TENANT_ADMIN), id="tenant-admin"),
    pytest.param(reader(TenantRole.TENANT_ADMIN, kind=PrincipalKind.SERVICE), id="service"),
    pytest.param(reader(TenantRole.TENANT_ADMIN, kind=PrincipalKind.AGENT), id="agent"),
]


def _shows_record(value: Any, sha: str) -> bool:
    text = value if isinstance(value, str) else json.dumps(value)
    return _KEY in text or sha in text


async def _ask(client: TestClient, key: Any) -> tuple[uuid.UUID, dict[str, Any], str]:
    """The tenant admin asks for the import; the request id, the stored card and its record."""
    await h.store_install(copy.deepcopy(_INSTALL))
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        asked = h.post(client, "import", a.tenant_admin(key), _FILE_RULE)
    assert asked.status_code == 202, asked.text
    (request,) = await h.pending_requests()
    card: dict[str, Any] = copy.deepcopy(request.proposed_effect["preview"])
    sha = card[_KEY]
    assert isinstance(sha, str)
    assert len(sha) == 64
    return request.id, card, sha


def _preview(client: TestClient, card: dict[str, Any]) -> Any:
    params = {
        "scope": "tenant",
        "file": _FILE_RULE,
        "file_sha256": card["file_sha256"],
        "based_on": card["based_on"],
    }
    arguments = {"connector_id": h.CONNECTOR, "op_id": POLICY_APPLY_OP_ID, "params": params}
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "preview_operation", "arguments": arguments},
    }
    return client.post("/mcp", json=body).json()


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize("who", _READERS)
async def test_no_rest_or_mcp_view_shows_the_recorded_install_value(
    client: TestClient, embedding: AsyncMock, as_reader: Any, who: Operator
) -> None:
    await h.setup_ops(embedding)
    request_id, stored, sha = await _ask(client, make_rsa_keypair("kid-record-hidden-rest"))
    as_reader(who)
    (got_status, got), (list_status, listed) = rest_cards(client, request_id)
    mcp_got, mcp_card, mcp_listed, mcp_list = mcp_cards(client, request_id)
    preview = _preview(client, stored)

    assert (got_status, list_status) == (200, 200)
    cards = [got, *listed, mcp_card, *mcp_list]
    assert len(cards) == 4
    for card in cards:
        assert "runs_at_once" in card["diff"]
        assert _KEY not in card
        assert card["planned_with_custom_policy"] is True
    for answer in (mcp_got, mcp_listed, preview):
        assert not _shows_record(answer, sha), answer
    (request,) = await h.pending_requests()
    assert request.proposed_effect["preview"] == stored


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_preview_operation_gives_the_plan_without_the_recorded_install_value(
    client: TestClient, embedding: AsyncMock, as_reader: Any
) -> None:
    await h.setup_ops(embedding)
    _request_id, stored, sha = await _ask(client, make_rsa_keypair("kid-record-hidden-preview"))
    as_reader(reader(TenantRole.TENANT_ADMIN, platform_admin=True))
    answer = _preview(client, stored)

    assert answer["result"]["isError"] is False, answer
    assert "runs_at_once" in json.dumps(answer)
    assert not _shows_record(answer, sha), answer


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
@pytest.mark.parametrize("platform_admin", [True, False], ids=["platform-admin", "tenant-admin"])
async def test_the_console_page_and_the_bell_panel_show_no_recorded_install_value(
    client: TestClient, embedding: AsyncMock, platform_admin: bool
) -> None:
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-record-hidden-console")
    request_id, stored, sha = await _ask(client, key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        await console_session(
            client,
            key,
            sub="person-a",
            role=TenantRole.TENANT_ADMIN,
            platform_admin=platform_admin,
        )
        pages = console_pages(client, request_id)
    client.cookies.clear()

    for name, page in pages.items():
        assert page.status_code == 200, (name, page.text)
        assert POLICY_APPLY_OP_ID in page.text, name
        assert not _shows_record(page.text, sha), name
    assert "runs_at_once" in pages["detail"].text
    (request,) = await h.pending_requests()
    assert request.proposed_effect["preview"] == stored


@pytest.mark.asyncio
@pytest.mark.usefixtures("switches")
async def test_after_the_reads_the_click_runs_the_file_when_the_install_policy_is_the_same(
    client: TestClient, embedding: AsyncMock
) -> None:
    await h.setup_ops(embedding)
    key = make_rsa_keypair("kid-record-hidden-click")
    request_id, stored, _sha = await _ask(client, key)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        page = await a.console_get(client, key, f"/ui/approvals/{request_id}", sub="rev-b")
        clicked = await h.console_approve(
            client, key, request_id, sub="tenant-admin-b", platform_admin=False
        )

    assert page.status_code == 200, page.text
    assert clicked.status_code == 200, clicked.text
    tenant = await a.stored_tenant()
    assert tenant is not None
    assert [rule["name"] for rule in tenant["agents"]] == ["helper"]
    (request,) = await h.requests()
    assert request.proposed_effect["preview"] == stored
