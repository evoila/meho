# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO keeps who last changed an agent definition and who handed over a runbook run.

Both are people behind an agent's requests
(:mod:`meho_backplane.operations.people_behind`). A change by a machine
login keeps the person who was there.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.agents.schemas import AgentDefinitionUpdate
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.machine_logins import machine_subs, note_machine_login
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, RunbookRun
from meho_backplane.mcp.auth import verify_mcp_jwt_and_bind
from meho_backplane.operations.people_behind import find_people_behind
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import ReassignRunRequest
from meho_backplane.settings import get_settings
from meho_backplane.ui.auth.middleware import UISessionContext
from meho_backplane.ui.routes.agents.forms import submit_toggle

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000b0201")


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_an_edit_records_who_changed_the_definition() -> None:
    definition_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentDefinition(
                id=definition_id,
                tenant_id=_TENANT,
                name="nightly",
                identity_ref="agent:nightly",
                model_tier="standard",
                system_prompt="old",
                toolset={},
                turn_budget=5,
                created_by_sub="author-1",
            )
        )
        await session.commit()

    await AgentDefinitionService().update(
        _TENANT, "nightly", AgentDefinitionUpdate(system_prompt="new"), updated_by_sub="editor-1"
    )

    async with get_sessionmaker()() as session:
        row = await session.get(AgentDefinition, definition_id)
        assert row is not None
        assert row.updated_by_sub == "editor-1"
        assert row.created_by_sub == "author-1"


async def test_a_console_toggle_records_who_changed_the_definition() -> None:
    definition_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentDefinition(
                id=definition_id,
                tenant_id=_TENANT,
                name="weekly",
                identity_ref="agent:weekly",
                model_tier="standard",
                system_prompt="prompt",
                toolset={},
                turn_budget=5,
                enabled=False,
                created_by_sub="author-1",
            )
        )
        await session.commit()

    admin = Operator(
        sub="toggler-1",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.USER,
    )
    session_ctx = UISessionContext(
        session_id=uuid.uuid4(), operator_sub="toggler-1", tenant_id=_TENANT
    )
    await submit_toggle(session_ctx, admin, name="weekly", enabled=True)

    async with get_sessionmaker()() as session:
        row = await session.get(AgentDefinition, definition_id)
        assert row is not None
        assert row.enabled is True
        assert row.updated_by_sub == "toggler-1"
        assert row.created_by_sub == "author-1"


async def test_a_handover_records_who_handed_the_run_over() -> None:
    run = RunbookRun(
        tenant_id=_TENANT,
        template_slug="patch",
        template_version=1,
        assigned_to="first-assignee",
        target="host-1",
        params={},
        started_by="starter-1",
    )
    async with get_sessionmaker()() as session:
        session.add(run)
        await session.commit()
        run_id = run.run_id

    await RunbookRunService().reassign_run(
        _TENANT, "admin-1", run_id, ReassignRunRequest(new_assignee="agent-sa-sub")
    )

    async with get_sessionmaker()() as session:
        row = await session.get(RunbookRun, run_id)
        assert row is not None
        assert row.assigned_to == "agent-sa-sub"
        assert row.reassigned_by == "admin-1"


def _machine(sub: str) -> Operator:
    return Operator(
        sub=sub,
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.TENANT_ADMIN,
        principal_kind=PrincipalKind.AGENT,
    )


async def _signs_in(operator: Operator) -> None:
    async with get_sessionmaker()() as session:
        await note_machine_login(session, operator)
        await session.commit()


async def test_a_machine_edit_keeps_the_person_who_last_changed_the_definition() -> None:
    definition_id = uuid.uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentDefinition(
                id=definition_id,
                tenant_id=_TENANT,
                name="monthly",
                identity_ref="agent:monthly",
                model_tier="standard",
                system_prompt="first",
                toolset={},
                turn_budget=5,
                created_by_sub="author-1",
            )
        )
        await session.commit()
    machine = _machine("machine-editor-sa")
    await _signs_in(machine)

    service = AgentDefinitionService()
    await service.update(
        _TENANT, "monthly", AgentDefinitionUpdate(system_prompt="second"), updated_by_sub="editor-1"
    )
    await service.update(
        _TENANT, "monthly", AgentDefinitionUpdate(system_prompt="third"), updated_by_sub=machine.sub
    )

    async with get_sessionmaker()() as session:
        row = await session.get(AgentDefinition, definition_id)
        assert row is not None
        assert row.system_prompt == "third"
        assert row.updated_by_sub == "editor-1"
        people = await find_people_behind(
            session, tenant_id=_TENANT, sub="agent:monthly", client_id=None, kind="agent"
        )
    assert people is not None
    assert {(entry["sub"], entry["why"]) for entry in people} == {
        ("author-1", "agent_definition"),
        ("editor-1", "changed_agent_definition"),
    }


async def test_a_machine_handover_keeps_the_person_who_handed_the_run_over() -> None:
    run = RunbookRun(
        tenant_id=_TENANT,
        template_slug="patch",
        template_version=1,
        assigned_to="first-assignee",
        target="host-1",
        params={},
        started_by="starter-1",
    )
    async with get_sessionmaker()() as session:
        session.add(run)
        await session.commit()
        run_id = run.run_id
    machine = _machine("machine-handover-sa")
    await _signs_in(machine)

    service = RunbookRunService()
    await service.reassign_run(_TENANT, "admin-1", run_id, ReassignRunRequest(new_assignee="a-1"))
    await service.reassign_run(_TENANT, machine.sub, run_id, ReassignRunRequest(new_assignee="a-2"))

    async with get_sessionmaker()() as session:
        row = await session.get(RunbookRun, run_id)
        assert row is not None
        assert row.assigned_to == "a-2"
        assert row.reassigned_by == "admin-1"


async def test_an_mcp_call_notes_a_machine_login_and_not_a_person() -> None:
    person = Operator(
        sub="person-on-mcp",
        name=None,
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=PrincipalKind.USER,
    )
    machine = _machine("agent-on-mcp")

    for operator in (person, machine, machine):
        assert await verify_mcp_jwt_and_bind(operator) is operator

    async with get_sessionmaker()() as session:
        assert await machine_subs(session, _TENANT, ["person-on-mcp", "agent-on-mcp"]) == {
            "agent-on-mcp"
        }
