# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who MEHO finds behind an agent, service or runner login.

:func:`~meho_backplane.operations.people_behind.find_people_behind` looks in
the login's tenant. These tests store each kind of record and check who
counts: who registered or owns the login, who gave a grant in force, who
wrote or last changed an agent definition, who made a schedule or an event
source, and who started or handed over a runbook run.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from meho_backplane.auth.jwt import _operator_from_claims
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AddonPairing,
    AgentDefinition,
    AgentPermission,
    AgentPrincipal,
    AuditLog,
    EventSource,
    RunbookRun,
    RunnerPrincipal,
    ScheduledTrigger,
    ServicePrincipalGrant,
)
from meho_backplane.operations.people_behind import (
    find_people_behind,
    people_behind_line,
    people_behind_unknown,
)
from meho_backplane.settings import get_settings

_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000b0001")
_OTHER_TENANT = uuid.UUID("00000000-0000-0000-0000-0000000b0002")
_AGENT_SUB = "agent-sa-uuid"
_AGENT_CLIENT = "agent:triage"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _add(*rows: object) -> None:
    async with get_sessionmaker()() as session:
        session.add_all(rows)
        await session.commit()


async def _find(
    kind: str | None, *, sub: str = _AGENT_SUB, client_id: str | None = _AGENT_CLIENT
) -> list[dict[str, object]] | None:
    async with get_sessionmaker()() as session:
        return await find_people_behind(
            session, tenant_id=_TENANT, sub=sub, client_id=client_id, kind=kind
        )


def _pairs(people: list[dict[str, object]] | None) -> set[tuple[object, object]]:
    assert people is not None
    return {(entry["sub"], entry["why"]) for entry in people}


def _agent_principal() -> AgentPrincipal:
    return AgentPrincipal(
        tenant_id=_TENANT,
        name="triage",
        keycloak_client_id=_AGENT_CLIENT,
        keycloak_internal_id="kc-triage",
        owner_sub="owner-1",
        created_by_sub="registrar-1",
    )


def _definition(definition_id: uuid.UUID, *, updated_by: str | None = None) -> AgentDefinition:
    return AgentDefinition(
        id=definition_id,
        tenant_id=_TENANT,
        name=f"def-{definition_id.hex[-4:]}",
        identity_ref=_AGENT_CLIENT,
        model_tier="standard",
        system_prompt="p",
        toolset={},
        turn_budget=5,
        created_by_sub="author-1",
        updated_by_sub=updated_by,
    )


def _trigger(definition_id: uuid.UUID, *, kind: str, creator: str) -> ScheduledTrigger:
    now = datetime.now(UTC)
    return ScheduledTrigger(
        tenant_id=_TENANT,
        agent_definition_id=definition_id,
        kind=kind,
        cron_expr="*/5 * * * *" if kind == "cron" else None,
        event_filter={"source": {"kind": "sensor"}} if kind == "event" else None,
        status="cancelled",
        identity_sub=_AGENT_SUB,
        created_by_sub=creator,
        created_at=now,
        updated_at=now,
    )


def _event_source(creator: str) -> EventSource:
    return EventSource(
        tenant_id=_TENANT,
        name="am",
        slug=f"am-{creator}",
        kind="alertmanager",
        auth_strategy="basic",
        created_by_sub=creator,
    )


async def test_a_person_has_no_list() -> None:
    assert await _find("user") is None
    assert await _find(None) is None


async def test_an_agent_nobody_set_up_in_meho_is_not_known() -> None:
    people = await _find("agent")

    assert people == []
    assert people_behind_unknown("agent", people)
    assert people_behind_line("agent", people) == (
        "Set up by: not known. Only an Admin may approve."
    )


async def test_an_agent_counts_who_registered_owns_and_gave_grants_in_force() -> None:
    now = datetime.now(UTC)
    await _add(
        _agent_principal(),
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=_AGENT_CLIENT,
            op_pattern="a.*",
            verdict="auto-execute",
            created_by_sub="giver-live",
        ),
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=_AGENT_SUB,
            op_pattern="b.*",
            verdict="auto-execute",
            created_by_sub="giver-until-later",
            expires_at=now + timedelta(hours=1),
        ),
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=_AGENT_CLIENT,
            op_pattern="c.*",
            verdict="auto-execute",
            created_by_sub="giver-expired",
            expires_at=now - timedelta(hours=1),
        ),
        AgentPermission(
            tenant_id=_OTHER_TENANT,
            principal_sub=_AGENT_CLIENT,
            op_pattern="d.*",
            verdict="auto-execute",
            created_by_sub="giver-other-tenant",
        ),
    )

    people = await _find("agent")

    assert _pairs(people) == {
        ("registrar-1", "registered"),
        ("owner-1", "owns"),
        ("giver-live", "gave_grant"),
        ("giver-until-later", "gave_grant"),
    }
    assert not people_behind_unknown("agent", people)


async def test_a_scheduled_agent_counts_definition_and_schedule_people() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    await _add(_definition(first, updated_by="editor-1"), _definition(second))
    await _add(
        _trigger(first, kind="cron", creator="scheduler-1"),
        _trigger(second, kind="event", creator="scheduler-2"),
        _event_source("source-maker"),
    )

    people = await _find("agent")

    assert _pairs(people) == {
        ("author-1", "agent_definition"),
        ("editor-1", "changed_agent_definition"),
        ("scheduler-1", "made_schedule"),
        ("scheduler-2", "made_schedule"),
        ("source-maker", "made_event_source"),
    }


async def test_event_sources_count_only_for_an_agent_that_waits_for_events() -> None:
    definition_id = uuid.uuid4()
    await _add(_definition(definition_id))
    await _add(
        _trigger(definition_id, kind="cron", creator="scheduler-1"),
        _event_source("source-maker"),
    )

    people = await _find("agent")

    assert ("source-maker", "made_event_source") not in _pairs(people)


async def test_a_runbook_run_counts_who_started_and_handed_it_over() -> None:
    def _run(*, ended: bool, starter: str) -> RunbookRun:
        return RunbookRun(
            tenant_id=_TENANT,
            template_slug="t",
            template_version=1,
            assigned_to=_AGENT_SUB,
            target="host-1",
            params={},
            started_by=starter,
            reassigned_by=f"{starter}-handover",
            completed_at=datetime.now(UTC) if ended else None,
        )

    await _add(_run(ended=False, starter="starter-live"), _run(ended=True, starter="starter-done"))

    people = await _find("agent")

    assert _pairs(people) == {
        ("starter-live", "started_run"),
        ("starter-live-handover", "handed_over_run"),
    }


async def test_a_service_login_counts_pairing_and_standing_grants_in_force() -> None:
    await _add(
        AddonPairing(
            tenant_id=_TENANT,
            name="builder",
            keycloak_client_id="addon:builder",
            keycloak_internal_id="kc-builder",
            service_account_sub="svc-sub",
            owner_sub="pair-owner",
            contract_version=1,
            addon_contract_version=1,
            addon_min_backplane_version=1,
            created_by_sub="pair-maker",
        ),
        ServicePrincipalGrant(
            tenant_id=_TENANT,
            principal_sub="svc-sub",
            op_id="x.create",
            connector_id="x-1.0",
            reason="build",
            created_by_sub="grant-live",
        ),
        ServicePrincipalGrant(
            tenant_id=_TENANT,
            principal_sub="svc-sub",
            op_id="x.delete",
            connector_id="x-1.0",
            reason="build",
            created_by_sub="grant-revoked",
            revoked_at=datetime.now(UTC),
            revoked_by_sub="admin",
        ),
    )

    people = await _find("service", sub="svc-sub", client_id=None)

    assert _pairs(people) == {
        ("pair-maker", "registered"),
        ("pair-owner", "owns"),
        ("grant-live", "gave_grant"),
    }


async def test_a_runner_counts_who_registered_and_owns_it() -> None:
    await _add(
        RunnerPrincipal(
            tenant_id=_TENANT,
            name="edge",
            keycloak_client_id="runner:edge",
            keycloak_internal_id="kc-edge",
            owner_sub="runner-owner",
            created_by_sub="runner-maker",
        )
    )

    people = await _find("runner", sub="runner-sa", client_id="runner:edge")

    assert _pairs(people) == {("runner-maker", "registered"), ("runner-owner", "owns")}


async def test_a_runner_token_finds_its_record_by_the_runner_id_claim() -> None:
    """A runner token has no username and no client id; its runner_id finds the record."""
    runner_id = uuid.uuid4()
    await _add(
        RunnerPrincipal(
            id=runner_id,
            tenant_id=_TENANT,
            name="edge",
            keycloak_client_id="runner:edge",
            keycloak_internal_id="kc-edge",
            owner_sub="runner-owner",
            created_by_sub="runner-maker",
        )
    )
    operator = _operator_from_claims(
        {
            "sub": "9f1c2d3e-runner-service-account",
            "azp": "runner:edge",
            "tenant_id": str(_TENANT),
            "tenant_role": "operator",
            "principal_kind": "runner",
            "runner_id": str(runner_id),
        },
        "raw.jwt.token",
        get_settings(),
    )
    assert operator.client_id is None

    async with get_sessionmaker()() as session:
        people = await find_people_behind(
            session,
            tenant_id=operator.tenant_id,
            sub=operator.sub,
            client_id=operator.client_id,
            kind=operator.principal_kind.value,
            runner_id=operator.runner_id,
        )

    assert _pairs(people) == {("runner-maker", "registered"), ("runner-owner", "owns")}


async def test_names_come_from_the_audit_log_and_the_requester_is_never_listed() -> None:
    await _add(
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=_AGENT_CLIENT,
            op_pattern="a.*",
            verdict="auto-execute",
            created_by_sub="giver-1",
        ),
        AgentPermission(
            tenant_id=_TENANT,
            principal_sub=_AGENT_SUB,
            op_pattern="b.*",
            verdict="auto-execute",
            created_by_sub=_AGENT_CLIENT,
        ),
        AuditLog(
            operator_sub="giver-1",
            method="POST",
            path="/api/v1/agents/grants",
            status_code=201,
            payload={"principal_name": "Grace Giver"},
            tenant_id=_TENANT,
        ),
    )

    people = await _find("agent")

    assert people == [{"sub": "giver-1", "name": "Grace Giver", "why": "gave_grant"}]
    assert people_behind_line("agent", people) == "Grant by Grace Giver."
