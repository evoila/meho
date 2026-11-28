# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The approval card of an install import, and the run after the click.

* The card of a waiting import of the install's file shows the ``limits``
  part of the plan (added, changed, removed), read with the same
  ``CUSTOM_POLICY`` switch as the run. With the switch off, the card has no
  ``limits`` section. After the click the stored policy holds what the card
  showed.
* An import planned with the switch on (of the install's file or of a
  tenant's file) holds ``planned_with_custom_policy: true``. A card planned
  with the switch off has the keys it had before the switch existed, also in
  ``meho_approvals_get`` and ``meho_approvals_list``. A card without the field was planned with the
  switch off. When the switch at the run is not the same as on the card, the
  run stores nothing and asks for a new preview.

Every case asks with a token (``POST /api/v1/policy/import?scope=install``)
and approves with a click in the web console
(``POST /ui/approvals/{request_id}/approve``).
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import PolicyRevision
from meho_backplane.mcp.tools.approvals import _get_handler, _list_handler
from meho_backplane.policy.service import POLICY_CHANGED
from meho_backplane.settings import get_settings

from ._install_limits_helpers import (
    FILE_P,
    audit_rows,
    client_fixture,  # noqa: F401
    console_approve,
    dispatch_apply,
    embedding_fixture,  # noqa: F401
    pending_requests,
    person,
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

_DANGEROUS = "limits: {personal_agent_up_to: dangerous}\n"
_CAUTION = "limits: {personal_agent_up_to: caution}\n"
_EDIT = FILE_P.replace("policy: v1\n", "policy: v1\ndescription: an unrelated edit\n")


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    if on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    get_settings.cache_clear()


async def _stored_limits() -> Any:
    return (await stored_install() or {}).get("limits")


async def _revisions() -> int:
    async with get_sessionmaker()() as session:
        return int((await session.execute(select(func.count(PolicyRevision.id)))).scalar_one())


async def _ask(
    client: TestClient, key: Any, content: str, scope: str = "install"
) -> tuple[uuid.UUID, dict[str, Any]]:
    """A platform admin asks by token; return the request and its card."""
    asked = post(client, "import", token(key, sub="admin-a"), content, scope=scope)
    assert asked.status_code == 202, asked.text
    (request,) = await pending_requests()
    assert str(request.id) == asked.json()["approval_request_id"]
    card = request.proposed_effect["preview"]
    assert card["based_on"] == request.params["based_on"]
    assert card["mistakes"] == []
    return request.id, card


# ---------------------------------------------------------------------------
# The card shows the ``limits`` part that the run stores
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stored", "asked", "card_limits", "after"),
    [
        pytest.param(
            None,
            FILE_P + _CAUTION,
            {
                "added": [{"key": "personal_agent_up_to", "value": "caution"}],
                "changed": [],
                "removed": [],
            },
            {"personal_agent_up_to": "caution"},
            id="1-added",
        ),
        pytest.param(
            FILE_P + _DANGEROUS,
            FILE_P + _CAUTION,
            {
                "added": [],
                "changed": [
                    {"key": "personal_agent_up_to", "before": "dangerous", "after": "caution"}
                ],
                "removed": [],
            },
            {"personal_agent_up_to": "caution"},
            id="2-changed",
        ),
        pytest.param(
            FILE_P + _DANGEROUS,
            _EDIT,
            {
                "added": [],
                "changed": [],
                "removed": [{"key": "personal_agent_up_to", "value": "dangerous"}],
            },
            None,
            id="3-removed",
        ),
    ],
)
async def test_the_card_shows_the_limits_part_that_the_run_stores(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    stored: str | None,
    asked: str,
    card_limits: dict[str, Any],
    after: Any,
) -> None:
    _switch(monkeypatch, True)
    await setup_ops(embedding)
    if stored is not None:
        assert (await dispatch_apply(person("admin-a"), stored)).status == "ok"
    key = make_rsa_keypair("kid-card-limits")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id, card = await _ask(client, key, asked)
        assert card["diff"]["limits"] == card_limits
        clicked = await console_approve(client, key, request_id, sub="admin-b")
    assert clicked.status_code == 200, clicked.text
    assert await pending_requests() == []
    assert await _stored_limits() == after


@pytest.mark.asyncio
async def test_with_the_switch_off_the_card_has_no_limits_section(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, True)
    await setup_ops(embedding)
    assert (await dispatch_apply(person("admin-a"), FILE_P + _DANGEROUS)).status == "ok"
    _switch(monkeypatch, False)
    key = make_rsa_keypair("kid-card-off")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        _request_id, card = await _ask(client, key, _EDIT)
    assert "limits" not in card["diff"]
    assert "planned_with_custom_policy" not in card


# ---------------------------------------------------------------------------
# The keys of the card, also as an agent reads it
# ---------------------------------------------------------------------------

_RELEASED_KEYS = {
    "scope",
    "file_sha256",
    "based_on",
    "planned_against_current",
    "mistakes",
    "warnings",
    "no_change",
    "diff",
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scope", "on", "field"),
    [
        pytest.param("tenant", False, None, id="15-tenant-off"),
        pytest.param("install", False, None, id="16-install-off"),
        pytest.param("install", True, True, id="17-install-on"),
        pytest.param("tenant", True, True, id="18-tenant-on"),
    ],
)
async def test_only_a_card_planned_with_the_switch_on_holds_the_field(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
    on: bool,
    field: bool | None,
) -> None:
    _switch(monkeypatch, on)
    await setup_ops(embedding)
    key = make_rsa_keypair("kid-card-keys")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id, card = await _ask(client, key, FILE_P, scope=scope)

    expected = _RELEASED_KEYS if field is None else _RELEASED_KEYS | {"planned_with_custom_policy"}
    stored_keys = expected
    if field is not None and scope == "tenant":
        stored_keys = expected | {"planned_with_install_limits"}
    reader = person("admin-b")
    got = (await _get_handler(reader, {"approval_request_id": str(request_id)}))["proposed_effect"][
        "preview"
    ]
    listed = await _list_handler(reader, {"status": "pending"})
    (in_list,) = [
        item["proposed_effect"]["preview"]
        for item in listed["items"]
        if item["id"] == str(request_id)
    ]
    assert set(card) == stored_keys
    for shown in (card, got, in_list):
        assert set(shown) - {"planned_with_install_limits"} == expected
        assert shown.get("planned_with_custom_policy") is field
    for shown in (got, in_list):
        assert "planned_with_install_limits" not in shown


# ---------------------------------------------------------------------------
# The switch at the run is the same as on the card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("at_ask", "at_run", "card_limits", "stores", "after"),
    [
        pytest.param(
            False, True, None, False, {"personal_agent_up_to": "dangerous"}, id="5-and-20"
        ),
        pytest.param(
            True,
            False,
            {
                "added": [],
                "changed": [],
                "removed": [{"key": "personal_agent_up_to", "value": "dangerous"}],
            },
            False,
            {"personal_agent_up_to": "dangerous"},
            id="6-and-21",
        ),
        pytest.param(
            True,
            True,
            {
                "added": [],
                "changed": [],
                "removed": [{"key": "personal_agent_up_to", "value": "dangerous"}],
            },
            True,
            None,
            id="7-and-22",
        ),
        pytest.param(
            False, False, None, True, {"personal_agent_up_to": "dangerous"}, id="8-and-19"
        ),
    ],
)
async def test_the_run_stores_nothing_when_the_switch_is_not_the_same_as_on_the_card(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    at_ask: bool,
    at_run: bool,
    card_limits: dict[str, Any] | None,
    stores: bool,
    after: Any,
) -> None:
    _switch(monkeypatch, True)
    await setup_ops(embedding)
    assert (await dispatch_apply(person("admin-a"), FILE_P + _DANGEROUS)).status == "ok"
    revisions = await _revisions()

    _switch(monkeypatch, at_ask)
    key = make_rsa_keypair("kid-card-switch")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        request_id, card = await _ask(client, key, _EDIT)
        # A card planned with the switch off has no field, like a card that
        # waited during an upgrade to this MEHO.
        assert card.get("planned_with_custom_policy") is (True if at_ask else None)
        assert card["diff"].get("limits") == card_limits
        _switch(monkeypatch, at_run)
        clicked = await console_approve(client, key, request_id, sub="admin-b")
    assert clicked.status_code == 200, clicked.text

    (run_row,) = [
        row
        for row in await audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        if row.operator_sub == "admin-b"
    ]
    assert len(await requests()) == 1
    assert await _stored_limits() == after
    if stores:
        assert run_row.status_code == 200
        assert await _revisions() == revisions + 1
        assert (await stored_install() or {}).get("description") == "an unrelated edit"
    else:
        assert run_row.status_code != 200
        assert POLICY_CHANGED in str(run_row.payload)
        assert await _revisions() == revisions
        assert "description" not in (await stored_install() or {})
