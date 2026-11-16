# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The console's audit row names the operation and the starting grant pick.

The console create, edit and register forms bind the same ``audit_*``
values as the REST routes, so a grant written from the console is as
easy to review as one written through the API. They bind the operation
before they read the pick, so a form refused early still has it.

The fixtures come from ``test_ui_agents.py`` (the autouse ones are
imported by name so pytest applies them here too).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import select
from starlette.middleware import Middleware

from meho_backplane.audit import AuditMiddleware
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from tests import test_ui_agents
from tests.test_ui_agent_starting_grants import (
    _CREATE,
    _REGISTER,
    _admin_client,
    _capture_register,
    _create_form,
    _seed_target,
    _switch,
)
from tests.test_ui_agent_starting_grants_edit import _patch, _seed_definition_and_new_agent
from tests.test_ui_agents import (  # noqa: F401 -- the autouse fixtures apply by import
    _TENANT_A,
    _bff_env,
    _csrf_headers,
    _seed_principal,
    _ui_operator_role,
)


@pytest.fixture(autouse=True)
def _audited_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Add the audit middleware to the test app, closest to the routes as in production."""
    build = test_ui_agents._build_app

    def _build() -> FastAPI:
        app = build()
        # ``add_middleware`` puts a middleware outermost; appending puts it innermost.
        app.user_middleware.append(Middleware(AuditMiddleware))
        return app

    monkeypatch.setattr(test_ui_agents, "_build_app", _build)


def _audit_row(op_id: str) -> dict[str, Any]:
    async def _do() -> list[AuditLog]:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(AuditLog).order_by(AuditLog.occurred_at))
            return list(result.scalars().all())

    rows = [r.payload for r in asyncio.run(_do()) if r.payload.get("op_id") == op_id]
    assert len(rows) == 1, rows
    return rows[0]


def test_edit_audit_row_names_the_set_and_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    _seed_target("alpha-vault")
    response = _patch(
        "agent-new", starting_grants="read-targets", starting_grant_targets=["alpha-vault"]
    )
    assert response.status_code == 204, response.text
    audit = _audit_row("agent.edit")
    assert audit["op_class"] == "write"
    assert audit["agent_name"] == "triage"
    assert audit["starting_grants"] == "read-targets"
    assert audit["starting_grant_targets"] == ["alpha-vault"]


def test_edit_without_a_pick_records_not_chosen(monkeypatch: pytest.MonkeyPatch) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-old")
    assert response.status_code == 204, response.text
    audit = _audit_row("agent.edit")
    assert audit["starting_grants"] == "not-chosen"
    assert "starting_grant_targets" not in audit


def test_create_audit_row_names_the_set_and_targets() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    _seed_target("alpha-vault")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(
                starting_grants="read-targets", starting_grant_targets=["alpha-vault"]
            ),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    audit = _audit_row("agent.create")
    assert audit["op_class"] == "write"
    assert audit["agent_name"] == "triage"
    assert audit["starting_grants"] == "read-targets"
    assert audit["starting_grant_targets"] == ["alpha-vault"]


def test_register_audit_row_names_the_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={"name": "deploy-bot", "starting_grants": "read-everything"},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 204, response.text
    audit = _audit_row("agent_principal.register")
    assert audit["op_class"] == "write"
    assert audit["agent_principal_name"] == "deploy-bot"
    assert audit["starting_grants"] == "read-everything"


def test_edit_audit_row_names_the_agent_that_got_the_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-new", starting_grants="read-everything")
    assert response.status_code == 204, response.text
    audit = _audit_row("agent.edit")
    assert audit["starting_grant_agent"] == "agent-new"
    assert audit["starting_grants"] == "read-everything"


def test_edit_refused_before_the_service_names_the_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Targets without "Read some targets" fail while the pick is read."""
    _switch(monkeypatch, "on")
    _seed_definition_and_new_agent()
    response = _patch("agent-new", starting_grant_targets=["alpha-vault"])
    assert response.status_code == 200, response.text
    audit = _audit_row("agent.edit")
    assert audit["op_class"] == "write"
    assert audit["agent_name"] == "triage"


def test_create_refused_before_the_service_names_the_operation() -> None:
    client, mock, csrf = _admin_client()
    _seed_principal(tenant_id=_TENANT_A, keycloak_client_id="agent-triage")
    try:
        response = client.post(
            _CREATE,
            data=_create_form(starting_grant_targets=["alpha-vault"]),
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 200, response.text
    audit = _audit_row("agent.create")
    assert audit["agent_name"] == "triage"


def test_register_refused_before_the_service_names_the_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capture_register(monkeypatch)
    client, mock, csrf = _admin_client()
    try:
        response = client.post(
            _REGISTER,
            data={"name": "deploy-bot", "starting_grant_targets": ["alpha-vault"]},
            headers=_csrf_headers(csrf),
        )
    finally:
        mock.stop()
    assert response.status_code == 422, response.text
    audit = _audit_row("agent_principal.register")
    assert audit["agent_principal_name"] == "deploy-bot"
