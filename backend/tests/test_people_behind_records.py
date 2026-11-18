# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""MEHO keeps who last changed an agent definition and who handed over a runbook run.

Both are people behind an agent's requests
(:mod:`meho_backplane.operations.people_behind`).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest

from meho_backplane.agents.schemas import AgentDefinitionUpdate
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentDefinition, RunbookRun
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.runs_schemas import ReassignRunRequest
from meho_backplane.settings import get_settings

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
