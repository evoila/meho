# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A refused console create or edit answers 422; a saved one answers 204.

* The audit row of a refused form has status 422, so it does not read
  like a done change, and no grant is written.
* The ``#agents-modal-container`` hook swaps the 422 answer into the
  modal on the list page and on the detail page, so the errors show.
  It swaps only MEHO's own HTML form answer, never a JSON 422.
* A system prompt or turn budget that is too long for the form route
  answers a JSON 422; the form caps both fields in the browser.
* The audit row of a console create names the agent that gets the
  grants (``starting_grant_agent``).

The fixtures come from ``test_ui_agents.py`` and
``test_ui_agent_starting_grants_audit.py`` (the autouse ones are
imported by name so pytest applies them here too).
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import subprocess
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from tests.test_ui_agent_starting_grants import (
    _CREATE,
    _SWAP_DRIVER,
    _admin_client,
    _create_form,
    _grant_rows,
    _seed_target,
    _switch,
    requires_node,
)
from tests.test_ui_agent_starting_grants_audit import _audited_app  # noqa: F401 -- autouse
from tests.test_ui_agent_starting_grants_edit import (
    _edit_form,
    _patch,
    _seed_definition_and_new_agent,
)
from tests.test_ui_agents import (  # noqa: F401 -- the autouse fixtures apply by import
    _TENANT_A,
    _bff_env,
    _csrf_headers,
    _seed_principal,
    _ui_operator_role,
)


def _audit_row(op_id: str) -> AuditLog:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
            return list(result.scalars().all())

    rows = [r for r in asyncio.run(_do()) if r.payload.get("op_id") == op_id]
    assert len(rows) == 1, [r.payload for r in rows]
    return rows[0]


def _post_create(**extra: Any) -> Any:
    client, mock, csrf = _admin_client()
    try:
        return client.post(_CREATE, data=_create_form(**extra), headers=_csrf_headers(csrf))
    finally:
        mock.stop()


def test_refused_edit_audit_row_has_status_422_and_no_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch(
        "agent-new", starting_grants="read-targets", starting_grant_targets=["no-such-target"]
    )
    assert response.status_code == 422, response.text
    row = _audit_row("agent.edit")
    assert row.status_code == 422
    assert row.payload["starting_grant_agent"] == "agent-new"
    assert _grant_rows("agent-new") == []


def test_saved_edit_audit_row_has_status_204(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-new", starting_grants="read-everything")
    assert response.status_code == 204, response.text
    assert _audit_row("agent.edit").status_code == 204


def test_refused_create_audit_row_has_status_422_and_no_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    response = _post_create(
        starting_grants="read-targets", starting_grant_targets=["no-such-target"]
    )
    assert response.status_code == 422, response.text
    row = _audit_row("agent.create")
    assert row.status_code == 422
    assert _grant_rows("agent-triage") == []


def test_create_audit_row_names_the_agent_that_gets_the_grants() -> None:
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    _seed_target("alpha-vault")
    response = _post_create(starting_grants="read-targets", starting_grant_targets=["alpha-vault"])
    assert response.status_code == 204, response.text
    row = _audit_row("agent.create")
    assert row.status_code == 204
    assert row.payload["starting_grant_agent"] == "agent-triage"


def _swap_hook(page_text: str) -> str:
    container = re.search(r'<div\s+id="agents-modal-container"[^>]*>', page_text)
    assert container is not None, page_text
    hook = re.search(r'hx-on::before-swap="([^"]*)"', container.group(0))
    assert hook is not None, container.group(0)
    return html.unescape(hook.group(1))


@requires_node
@pytest.mark.parametrize("path", ["/ui/agents", "/ui/agents/triage"])
def test_the_modal_container_swaps_a_422_answer_in(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    client, mock, csrf = _admin_client()
    try:
        page = client.get(path)
        refused = client.request(
            "PATCH", "/ui/agents/triage", data=_edit_form("agent-new"), headers=_csrf_headers(csrf)
        )
    finally:
        mock.stop()
    assert page.status_code == 200, page.text
    hook = _swap_hook(page.text)
    assert refused.status_code == 422, refused.text

    cases = [
        {
            "name": "edit422",
            "status": 422,
            "verb": "patch",
            "body": refused.text,
            "contentType": refused.headers["content-type"],
        },
        {"name": "create422", "status": 422, "verb": "post", "body": "<dialog>422</dialog>"},
        {
            "name": "json422",
            "status": 422,
            "verb": "post",
            "body": '{"detail": []}',
            "contentType": "application/json",
        },
        {"name": "ok200", "status": 200, "verb": "get", "body": "<dialog>ok</dialog>"},
        {"name": "get404", "status": 404, "verb": "get", "body": "not found"},
        {"name": "post500", "status": 500, "verb": "post", "body": "boom"},
    ]
    proc = subprocess.run(
        ["node", "-e", _SWAP_DRIVER, hook, json.dumps(cases)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["edit422"]["swapped"] is True
    assert 'data-error-for="starting_grants"' in result["edit422"]["container"]
    assert result["create422"]["swapped"] is True
    # A JSON 422 (a field too long for the form route) is not MEHO's form:
    # the open form and the person's text stay.
    assert result["json422"]["swapped"] is False
    assert result["ok200"]["swapped"] is True
    # Other errors keep htmx's default: no swap.
    assert result["get404"]["swapped"] is False
    assert result["post500"]["swapped"] is False
