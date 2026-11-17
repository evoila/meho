# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The agent form shows the agent's live grants next to the starting grant pick (#6584).

* ``GET /ui/agents/principals/live-grants`` lists the live grants of the
  agent that the identity reference names, with the sentence "This agent
  already has these grants; no starting grant is needed." A grant that
  has run out is not listed. Another tenant's grants are not listed.
* The create and edit forms load that fragment from the identity
  reference field.
* The form caps the system prompt and the turn budget in the browser, so
  the server's length check (a JSON 422) is not reached by normal typing.

The fixtures come from ``test_ui_agents.py``.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentPermission
from tests.test_ui_agent_starting_grants import _CREATE, _admin_client, _create_form, _seed_target
from tests.test_ui_agent_starting_grants_edit import _seed_definition_and_new_agent
from tests.test_ui_agents import (  # noqa: F401 -- the autouse fixtures apply by import
    _OP_A,
    _TENANT_A,
    _TENANT_B,
    _authenticated_client,
    _bff_env,
    _csrf_headers,
    _make_keypair_and_jwks,
    _operator_session,
    _seed_session_sync,
    _seed_tenant,
    _ui_operator_role,
)

_PATH = "/ui/agents/principals/live-grants"
_NOTE = "This agent already has these grants; no starting grant is needed."


def _add_grant(
    sub: str,
    *,
    op_pattern: str = "vault.*",
    target_scope: str = "*",
    expires_at: datetime | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
) -> None:
    async def _do() -> None:
        async with get_sessionmaker()() as session:
            session.add(
                AgentPermission(
                    tenant_id=tenant_id,
                    principal_sub=sub,
                    op_pattern=op_pattern,
                    target_scope=target_scope,
                    verdict="auto-execute",
                    created_by_sub=_OP_A,
                    expires_at=expires_at,
                )
            )
            await session.commit()

    asyncio.run(_do())


def _fragment(identity_ref: str) -> Any:
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        return client.get(_PATH, params={"identity_ref": identity_ref})
    finally:
        mock.stop()


def test_one_live_grant_shows_with_the_note() -> None:
    _add_grant("agent-triage")
    response = _fragment("agent-triage")
    assert response.status_code == 200, response.text
    assert _NOTE in response.text
    assert "vault.*" in response.text
    assert "every target" in response.text
    assert ">never<" in response.text


def test_an_all_operations_grant_on_a_target_shows_the_target_name() -> None:
    target_id = _seed_target("alpha-vault")
    _add_grant("agent-triage", op_pattern="*", target_scope=target_id)
    response = _fragment("agent-triage")
    assert _NOTE in response.text
    assert "alpha-vault" in response.text
    assert target_id not in response.text


def test_a_grant_with_an_end_date_shows_the_date() -> None:
    ends = datetime.now(UTC) + timedelta(days=2)
    _add_grant("agent-triage", expires_at=ends)
    response = _fragment("agent-triage")
    assert _NOTE in response.text
    match = re.search(r'data-expires-at="([^"]+)"', response.text)
    assert match is not None, response.text
    assert datetime.fromisoformat(match.group(1)) == ends


def test_a_grant_that_ran_out_is_not_shown() -> None:
    _add_grant("agent-triage", expires_at=datetime.now(UTC) - timedelta(days=1))
    response = _fragment("agent-triage")
    assert response.status_code == 200, response.text
    assert _NOTE not in response.text
    assert "data-live-grants" not in response.text


def test_another_tenants_grant_is_not_shown() -> None:
    _seed_tenant(_TENANT_B, "tenant-b")
    _add_grant("agent-triage", tenant_id=_TENANT_B)
    response = _fragment("agent-triage")
    assert _NOTE not in response.text


def test_an_empty_identity_ref_shows_nothing() -> None:
    response = _fragment("   ")
    assert response.status_code == 200, response.text
    assert "data-live-grants" not in response.text


def test_the_fragment_is_for_a_tenant_admin_only() -> None:
    _seed_tenant(_TENANT_A, "tenant-a")
    keypair, jwks = _make_keypair_and_jwks()
    token = _operator_session(keypair)
    session_id = _seed_session_sync(tenant_id=_TENANT_A, access_token=token, operator_sub=_OP_A)
    client, mock, _csrf = _authenticated_client(session_id=session_id, jwks=jwks)
    try:
        response = client.get(_PATH, params={"identity_ref": "agent-triage"})
    finally:
        mock.stop()
    assert response.status_code == 403


def _form(path: str) -> str:
    client, mock, _csrf = _admin_client(with_csrf=False)
    try:
        response = client.get(path)
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    return response.text


@pytest.mark.parametrize("path", [_CREATE, "/ui/agents/triage/edit"])
def test_the_forms_load_the_live_grants_from_the_identity_ref_field(path: str) -> None:
    _seed_definition_and_new_agent()
    body = _form(path)
    field = re.search(r'<input[^>]*name="identity_ref"[^>]*>', body, re.DOTALL)
    assert field is not None, body
    assert f'hx-get="{_PATH}"' in field.group(0)
    assert 'hx-target="#agent-live-grants"' in field.group(0)
    assert "load" in field.group(0)
    assert 'id="agent-live-grants"' in body


@pytest.mark.parametrize("path", [_CREATE, "/ui/agents/triage/edit"])
def test_the_forms_cap_the_system_prompt_and_turn_budget(path: str) -> None:
    _seed_definition_and_new_agent()
    body = _form(path)
    prompt = re.search(r'<textarea[^>]*name="system_prompt"[^>]*>', body, re.DOTALL)
    assert prompt is not None and 'maxlength="65536"' in prompt.group(0)
    budget = re.search(r'<input[^>]*name="turn_budget"[^>]*>', body, re.DOTALL)
    assert budget is not None and 'maxlength="8"' in budget.group(0)


def test_a_too_long_system_prompt_is_a_json_422_and_saves_nothing() -> None:
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _CREATE,
            data=_create_form(system_prompt="x" * (64 * 1024 + 1)),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422
    # Not MEHO's HTML form: the modal hook does not swap it in.
    assert response.headers["content-type"].startswith("application/json")
