# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The starting grant pick in the console agent definition forms follows the API rule (#6389).

* The create form never marks the pick required: the service asks for it
  only when the agent has no live grant.
* The edit form asks for it when ``identity_ref`` changes to an agent
  with no grant, the same as ``PATCH /api/v1/agents/{name}``.

The fixtures come from ``test_ui_agents.py`` (the autouse ones are
imported by name so pytest applies them here too).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, AgentPermission
from tests.test_ui_agent_starting_grants import (
    _CREATE,
    _admin_client,
    _create_form,
    _definition_names,
    _grant_rows,
    _radios,
    _switch,
)
from tests.test_ui_agents import (  # noqa: F401 -- the autouse fixtures apply by import
    _OP_A,
    _TENANT_A,
    _bff_env,
    _csrf_headers,
    _seed_agent,
    _seed_principal,
    _ui_operator_role,
)


def _seed_live_grant(principal_sub: str) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session, session.begin():
            session.add(
                AgentPermission(
                    tenant_id=_TENANT_A,
                    principal_sub=principal_sub,
                    op_pattern="vault.*",
                    target_scope="*",
                    verdict="auto-execute",
                    created_by_sub=_OP_A,
                )
            )

    asyncio.run(_do())


def _identity_ref_of(name: str) -> str:
    async def _do() -> str:
        async with get_sessionmaker()() as session:
            result = await session.execute(
                select(AgentDefinition.identity_ref).where(AgentDefinition.name == name)
            )
            return result.scalar_one()

    return asyncio.run(_do())


def _pick_error(body: str) -> str | None:
    match = re.search(r'data-error-for="starting_grants">\s*([^<]+)<', body)
    return match.group(1) if match else None


# ---------------------------------------------------------------------------
# Create form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["observe", "on"])
def test_create_form_does_not_mark_the_pick_required(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _switch(monkeypatch, mode)
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        body = client.get(_CREATE).text
    finally:
        mock.stop()
    radios = _radios(body)
    assert len(radios) == 3
    assert not any("required" in r for r in radios)
    assert "MEHO needs this pick when the agent has no grants yet." in body


def test_create_for_an_agent_with_a_grant_needs_no_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    _seed_live_grant("agent-triage")
    try:
        response = client.post(_CREATE, data=_create_form(), headers=_csrf_headers(csrf))
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents"
    assert _definition_names() == ["triage"]
    assert len(_grant_rows("agent-triage")) == 1


# ---------------------------------------------------------------------------
# Edit form
# ---------------------------------------------------------------------------


def _edit_form(identity_ref: str, **extra: Any) -> dict[str, Any]:
    return {
        "identity_ref": identity_ref,
        "model_tier": "standard",
        "system_prompt": "You triage.",
        "turn_budget": "10",
        "enabled": "true",
        **extra,
    }


def _patch(identity_ref: str, **extra: Any) -> Any:
    client, mock, csrf = _admin_client()
    try:
        return client.request(
            "PATCH",
            "/ui/agents/triage",
            data=_edit_form(identity_ref, **extra),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()


def _seed_definition_and_new_agent() -> None:
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-old")
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-new")
    _seed_agent(tenant_id=_TENANT_A, name="triage", identity_ref="agent-old")


def test_edit_to_an_agent_without_a_grant_shows_the_pick_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-new")
    # 200 so HTMX swaps the form back in.
    assert response.status_code == 200, response.text
    error = _pick_error(response.text)
    assert error is not None
    assert "Choose &#39;none&#39;" in error
    assert "starting_grants_required" not in error
    assert _grant_rows("agent-new") == []
    assert _identity_ref_of("triage") == "agent-old"


def test_edit_to_an_agent_without_a_grant_with_a_pick_writes_the_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-new", starting_grants="read-everything")
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents/grants?principal_sub=agent-new"
    rows = _grant_rows("agent-new")
    assert [(r.op_pattern, r.target_scope, r.created_by_sub) for r in rows] == [("*", "*", _OP_A)]
    assert _identity_ref_of("triage") == "agent-new"


def test_edit_that_keeps_the_agent_needs_no_pick(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-old")
    assert response.status_code == 204, response.text
    assert response.headers["HX-Redirect"] == "/ui/agents"
    assert _grant_rows("agent-old") == []


def test_edit_unknown_target_keeps_the_pick_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch(
        "agent-new", starting_grants="read-targets", starting_grant_targets=["no-such-target"]
    )
    assert response.status_code == 200, response.text
    error = _pick_error(response.text)
    assert error is not None
    assert "no target named" in error
    assert 'value="read-targets"' in response.text
    assert _grant_rows("agent-new") == []
    assert _identity_ref_of("triage") == "agent-old"
