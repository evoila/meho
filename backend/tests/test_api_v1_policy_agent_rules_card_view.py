# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How each reader sees the approval card of a tenant import with personal agent rules.

A tenant admin asks for the import with a token (``POST /api/v1/policy/import``).
Then each reader reads the card on its own path: a person in the web console
(``GET /ui/approvals/{request_id}``) and over REST with a person's token; an
agent and a service login over REST (``GET /api/v1/approvals`` and
``GET /api/v1/approvals/{request_id}``), and an agent and a service login
through ``POST /mcp`` (``meho_approvals_get`` and ``meho_approvals_list``).

* A person gets each role's whole list over REST and in the web console.
  Every MCP answer (``meho_approvals_get``, ``meho_approvals_list`` and
  ``preview_operation`` of ``policy.apply``, also for a person) and a caller
  that is not a person over REST get, for each role, the count, the first
  50 items, and "N more" when there are more. A runner login reads no
  approval card.
* The policy history (``GET /api/v1/policy/history``) gives its entries in
  the same way: a person gets the whole list, a service login the short
  form. With ``CUSTOM_POLICY`` off, an entry's ``diff`` has no part that
  only the switch adds, and the stored revision stays as it is.
* With ``CUSTOM_POLICY`` off, every view shows the card without the parts
  that only the switch adds. The stored row stays as it is, and a click
  stores nothing. A card asked with the switch off reads the same in every
  view, before and after the switch changes.

Drives the production ``meho_backplane.main:app``. DB is the autouse
per-test SQLite + ``alembic upgrade head``.
"""

from __future__ import annotations

import copy
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest
import respx
from fastapi.testclient import TestClient
from sqlalchemy import select

from meho_backplane.connectors.policy.apply import POLICY_APPLY_OP_ID
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, PolicyRevision
from meho_backplane.mcp.registry import MCP_ADMIN_SCOPE
from meho_backplane.policy.service import POLICY_CHANGED
from meho_backplane.settings import get_settings

from . import _agent_rules_helpers as a
from . import _install_limits_helpers as h
from ._install_limits_helpers import (
    client_fixture,  # noqa: F401
    embedding_fixture,  # noqa: F401
)
from ._oidc_jwt_helpers import make_rsa_keypair, mint_token, mock_discovery_and_jwks, public_jwks
from ._targets_helpers import (
    _empty_connector_registry,  # noqa: F401
    _isolated_jwks_cache,  # noqa: F401
    _settings_env,  # noqa: F401
)

_KEYPAIR = make_rsa_keypair("kid-agents-card-view")
_MCP_URI = "https://meho.test/mcp"

_FILE_BIG = (
    "policy: v1\nagents:\n  - name: helper\n    caller: personal_agent\n"
    "    roles: [Admin, Operator]\n    up_to: dangerous\n    connectors: [fxbig-1.x]\n"
)

#: The parts of the card that only the switch adds.
_SWITCHED = ("agents", "runs_at_once", "agents_above_limit")


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    monkeypatch.setenv("APPROVAL_SIGN_IN_LEVEL", "observe")
    monkeypatch.setenv("MCP_RESOURCE_URI", _MCP_URI)
    if on:
        monkeypatch.setenv("CUSTOM_POLICY", "true")
    else:
        monkeypatch.delenv("CUSTOM_POLICY", raising=False)
    get_settings.cache_clear()


async def _big_connector(count: int) -> None:
    """*count* ``caution`` operations ``fxbig.thing.op001`` and on, copies of a fixture row."""
    async with get_sessionmaker()() as session, session.begin():
        row = (
            await session.execute(
                select(EndpointDescriptor).where(EndpointDescriptor.op_id == "inslim.vm.restart")
            )
        ).scalar_one()
        values = {
            column.key: getattr(row, column.key)
            for column in EndpointDescriptor.__table__.columns
            if column.key not in ("id", "group_id")
        }
        for n in range(1, count + 1):
            op_id = f"fxbig.thing.op{n:03d}"
            names = {"product": "fxbig", "impl_id": "fxbig", "op_id": op_id}
            session.add(EndpointDescriptor(**{**values, **names}))


def _token(kind: str | None, *, mcp: bool = False) -> str:
    return mint_token(
        _KEYPAIR,
        sub=f"{kind or 'person'}-reader",
        tenant_id=str(h.TENANT),
        tenant_role="tenant_admin",
        principal_kind=kind,
        platform_admin=False,
        runner_id="00000000-0000-4000-8000-0000000000c1" if kind == "runner" else None,
        scopes=[MCP_ADMIN_SCOPE] if mcp else None,
        audience=_MCP_URI if mcp else None,
    )


def _rest_cards(client: TestClient, kind: str | None, request_id: str) -> list[dict[str, Any]]:
    """The card from ``GET /api/v1/approvals/{id}`` and from the list, for a *kind* login."""
    headers = {"Authorization": f"Bearer {_token(kind)}"}
    got = client.get(f"/api/v1/approvals/{request_id}", headers=headers)
    listed = client.get("/api/v1/approvals", params={"status": "pending"}, headers=headers)
    assert got.status_code == 200, got.text
    assert listed.status_code == 200, listed.text
    (in_list,) = [item for item in listed.json() if item["id"] == request_id]
    return [got.json()["proposed_effect"]["preview"], in_list["proposed_effect"]["preview"]]


def _mcp_call(client: TestClient, kind: str | None, name: str, arguments: dict[str, Any]) -> Any:
    resp = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers={"Authorization": f"Bearer {_token(kind, mcp=True)}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"]["isError"] is False, body
    return json.loads(body["result"]["content"][0]["text"])


def _mcp_cards(client: TestClient, kind: str | None, request_id: str) -> list[dict[str, Any]]:
    got = _mcp_call(client, kind, "meho_approvals_get", {"approval_request_id": request_id})
    listed = _mcp_call(client, kind, "meho_approvals_list", {"status": "pending"})
    (in_list,) = [item for item in listed["items"] if item["id"] == request_id]
    return [got["proposed_effect"]["preview"], in_list["proposed_effect"]["preview"]]


def _ask(client: TestClient, content: str) -> str:
    resp = h.post(client, "import", a.tenant_admin(_KEYPAIR), content)
    assert resp.status_code == 202, resp.text
    return str(resp.json()["approval_request_id"])


async def _stored_card() -> dict[str, Any]:
    (request,) = await h.pending_requests()
    return copy.deepcopy(request.proposed_effect["preview"])


# ---------------------------------------------------------------------------
# A caller who is not a person gets a short list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "more"), [(120, "70 more"), (50, None)], ids=["120", "50"])
async def test_a_person_gets_the_whole_list_and_another_caller_a_short_one(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    more: str | None,
) -> None:
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _big_connector(count)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        request_id = _ask(client, _FILE_BIG)
        stored = await _stored_card()
        page = await a.console_get(client, _KEYPAIR, f"/ui/approvals/{request_id}", sub="rev-b")
        person = _rest_cards(client, None, request_id)
        short = [_rest_cards(client, kind, request_id) for kind in ("agent", "service")]
        short += [_mcp_cards(client, kind, request_id) for kind in (None, "agent", "service")]
        runner = client.get(
            f"/api/v1/approvals/{request_id}",
            headers={"Authorization": f"Bearer {_token('runner')}"},
        )

    whole = stored["diff"]["runs_at_once"]
    assert [len(whole[role]) for role in ("Reader", "Operator", "Admin")] == [0, count, count]
    assert page.status_code == 200, page.text
    assert f"fxbig.thing.op{count:03d}" in page.text
    shown = {key: value for key, value in stored.items() if key != "planned_with_install_limits"}
    assert "planned_with_install_limits" in stored
    for card in person:
        assert card == shown
    expected = {
        role: {
            "count": len(items),
            "items": items[:50],
            **({"more": more} if more and items else {}),
        }
        for role, items in whole.items()
    }
    for cards in short:
        for card in cards:
            assert card["diff"]["runs_at_once"] == expected
            assert {**card, "diff": {**card["diff"], "runs_at_once": whole}} == shown
    assert runner.status_code == 403, runner.text
    assert await _stored_card() == stored


# ---------------------------------------------------------------------------
# A card read with the switch off
# ---------------------------------------------------------------------------


def _every_view(client: TestClient, request_id: str) -> list[dict[str, Any]]:
    cards = _rest_cards(client, None, request_id) + _rest_cards(client, "agent", request_id)
    return cards + _mcp_cards(client, "agent", request_id)


def _without_switched(card: dict[str, Any]) -> dict[str, Any]:
    switched = ("planned_with_custom_policy", "planned_with_install_limits")
    shown = {key: value for key, value in card.items() if key not in switched}
    shown["diff"] = {key: value for key, value in card["diff"].items() if key not in _SWITCHED}
    return shown


@pytest.mark.asyncio
async def test_with_the_switch_off_a_card_shows_no_switch_on_part_and_a_click_stores_nothing(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        request_id = _ask(client, a.FILE_A)
        stored = await _stored_card()
        assert stored["planned_with_custom_policy"] is True
        assert "runs_at_once" in stored["diff"]

        _switch(monkeypatch, False)
        views = _every_view(client, request_id)
        page = await a.console_get(client, _KEYPAIR, f"/ui/approvals/{request_id}", sub="rev-b")
        clicked = await h.console_approve(
            client, _KEYPAIR, request_id, sub="tenant-admin-b", platform_admin=False
        )

    for card in views:
        assert card == _without_switched(stored)
    assert page.status_code == 200, page.text
    for part in ("runs_at_once", "planned_with_custom_policy", "tenant_rule:helper"):
        assert part not in page.text
    assert clicked.status_code == 200, clicked.text
    (run_row,) = [
        row
        for row in await h.audit_rows("DISPATCH", POLICY_APPLY_OP_ID)
        if row.operator_sub == "tenant-admin-b"
    ]
    assert POLICY_CHANGED in str(run_row.payload)
    assert await a.stored_tenant() is None
    (request,) = await h.requests()
    assert request.proposed_effect["preview"] == stored


@pytest.mark.asyncio
async def test_a_card_asked_with_the_switch_off_reads_the_same_in_every_view(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, False)
    await h.setup_ops(embedding)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        request_id = _ask(client, a.FILE_NO_RULES)
        stored = await _stored_card()
        before = _every_view(client, request_id)
        _switch(monkeypatch, True)
        after = _every_view(client, request_id)

    assert "planned_with_custom_policy" not in stored
    assert not set(_SWITCHED) & set(stored["diff"])
    for card in before + after:
        assert card == stored


def _short_form(whole: dict[str, list[Any]], more: str | None) -> dict[str, Any]:
    return {
        role: {
            "count": len(items),
            "items": items[:50],
            **({"more": more} if more and items else {}),
        }
        for role, items in whole.items()
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "more"), [(120, "70 more"), (50, None)], ids=["120", "50"])
async def test_preview_operation_gives_a_person_the_short_form(
    client: TestClient,
    embedding: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
    count: int,
    more: str | None,
) -> None:
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _big_connector(count)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        planned = h.post(client, "import", a.tenant_admin(_KEYPAIR), _FILE_BIG, preview="true")
        assert planned.status_code == 200, planned.text
        params = {
            "scope": "tenant",
            "file": _FILE_BIG,
            "file_sha256": planned.json()["file_sha256"],
            "based_on": planned.json()["based_on"],
        }
        answer = _mcp_call(
            client,
            None,
            "preview_operation",
            {"connector_id": h.CONNECTOR, "op_id": POLICY_APPLY_OP_ID, "params": params},
        )

    whole = planned.json()["diff"]["runs_at_once"]
    assert [len(whole[role]) for role in ("Reader", "Operator", "Admin")] == [0, count, count]
    found = [card for card in _cards_in(answer) if "runs_at_once" in card.get("diff", {})]
    assert found, answer
    for card in found:
        assert card["diff"]["runs_at_once"] == _short_form(whole, more)


def _cards_in(value: Any) -> list[dict[str, Any]]:
    """Every dict with a ``diff`` key anywhere in *value*."""
    if isinstance(value, dict):
        own = [value] if isinstance(value.get("diff"), dict) else []
        return own + [card for item in value.values() for card in _cards_in(item)]
    if isinstance(value, list):
        return [card for item in value for card in _cards_in(item)]
    return []


def _history(client: TestClient, bearer: str) -> list[dict[str, Any]]:
    resp = client.get("/api/v1/policy/history", headers={"Authorization": f"Bearer {bearer}"})
    assert resp.status_code == 200, resp.text
    entries: list[dict[str, Any]] = resp.json()
    return entries


async def _revision_diffs() -> list[Any]:
    async with get_sessionmaker()() as session:
        return [
            copy.deepcopy(row.diff)
            for row in (await session.execute(select(PolicyRevision))).scalars()
        ]


@pytest.mark.asyncio
async def test_the_history_gives_a_service_login_the_short_form_and_hides_switch_parts_when_off(
    client: TestClient, embedding: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, True)
    await h.setup_ops(embedding)
    await a.set_limit("dangerous")
    await _big_connector(120)
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(_KEYPAIR))
        request_id = _ask(client, _FILE_BIG)
        clicked = await h.console_approve(
            client, _KEYPAIR, request_id, sub="tenant-admin-b", platform_admin=False
        )
        assert clicked.status_code == 200, clicked.text
        (stored_diff,) = await _revision_diffs()
        person = _history(client, a.tenant_admin(_KEYPAIR))
        service = _history(client, _token("service"))
        _switch(monkeypatch, False)
        person_off = _history(client, a.tenant_admin(_KEYPAIR))

    whole = stored_diff["runs_at_once"]
    assert len(whole["Admin"]) == 120
    ((entry,), (service_entry,), (off_entry,)) = (person, service, person_off)
    assert entry["diff"] == stored_diff
    assert service_entry["diff"]["runs_at_once"] == _short_form(whole, "70 more")
    assert {**service_entry["diff"], "runs_at_once": whole} == stored_diff
    assert not set(_SWITCHED) & set(off_entry["diff"])
    assert off_entry["diff"] == {k: v for k, v in stored_diff.items() if k not in _SWITCHED}
    assert await _revision_diffs() == [stored_diff]
