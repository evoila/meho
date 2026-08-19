# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Real-Postgres end-to-end for the event-subscription matcher (#2878).

The SQLite unit suite (:mod:`tests.test_event_matcher`) proves the matcher's
logic; this module runs the same chain -- publish an ``agent_run.completed``
event -> drain -> ``payload @> event_filter`` match -> fire an agent run with
the ``event:{event_id}:{trigger_id}`` work_ref -- against a real
``pgvector/pgvector:pg16`` container so the JSONB ``payload`` / ``event_filter``
round-trip, the drain's ``pg_try_advisory_lock`` + ``FOR UPDATE SKIP LOCKED``
mechanics, and the subscriber run committing on its own PG connection are all
exercised on the production dialect.

The ``pg_engine`` fixture from :mod:`tests.integration.conftest` points the
process-wide engine at the container and truncates the chassis tables between
tests; it skips the whole module when Docker is unavailable (agent sandboxes),
so this runs in CI where containers are provisioned.
"""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.events.drain import run_one_drain_tick
from tests.test_event_matcher import (
    _TENANT_A,
    _all_runs,
    _create_event_trigger,
    _is_processed,
    _make_invoker,
    _make_no_call_invoker,
    _publish_completed_event,
    _seed_tenant_and_agent,
    _wait_for_runs,
)


@pytest.fixture(autouse=True)
def _agent_fire_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub the autonomous-auth seams + the seeded agent's client secret.

    Layers on top of the integration conftest's ``_integration_default_env``
    (KEYCLOAK_* / VAULT_*) so ``run_scheduled`` runs offline against the seeded
    ``agent:reporter`` definition, exactly as :mod:`tests.test_event_matcher`
    does at the unit level.
    """
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "test-secret")
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.get_client_credentials_token",
        AsyncMock(return_value="agent-token"),
    )
    monkeypatch.setattr(
        "meho_backplane.agent.invocation.verify_jwt_for_audience",
        AsyncMock(
            return_value=Operator(
                sub=f"agent-{_TENANT_A.hex[:8]}",
                name=None,
                email=None,
                raw_jwt="agent-token",
                tenant_id=_TENANT_A,
                tenant_role=TenantRole.OPERATOR,
            ),
        ),
    )
    yield


async def test_matching_event_fires_run_with_work_ref_on_pg(pg_engine: None) -> None:
    """On real PG: a matching event fires a run whose work_ref keys the pair."""
    def_id = await _seed_tenant_and_agent()
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "follow up"},
    )
    event_id = await _publish_completed_event(status="succeeded")

    processed = await run_one_drain_tick(invoker=_make_invoker())
    assert processed == 1
    assert await _is_processed(event_id)

    runs = await _wait_for_runs(1)
    assert len(runs) == 1
    assert runs[0].work_ref == f"event:{event_id}:{trigger.id}"
    assert runs[0].tenant_id == _TENANT_A


async def test_non_matching_filter_fires_nothing_on_pg(pg_engine: None) -> None:
    """On real PG: an event that does not contain the filter drains without firing."""
    def_id = await _seed_tenant_and_agent()
    await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "follow up"},
    )
    event_id = await _publish_completed_event(status="failed")

    processed = await run_one_drain_tick(invoker=_make_no_call_invoker())
    assert processed == 1
    assert await _is_processed(event_id)
    assert await _all_runs() == []
