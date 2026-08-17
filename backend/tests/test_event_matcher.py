# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Behavioural tests for the G11.3 event-subscription matcher (#2878).

Coverage matrix mapped to the issue's acceptance criteria:

* **Containment direction** -- ``payload @> event_filter``: a filter that is a
  subset of the payload matches; a payload that is a subset of the filter does
  not. Pinned as a pure-predicate test plus an end-to-end fire.
* **End-to-end fire** -- an ``agent_run.completed`` event drains, matches a
  ``kind=event`` trigger, and fires an agent run whose ``work_ref`` is
  ``event:{event_id}:{trigger_id}``; a non-matching filter fires nothing.
* **Redelivery dedupe** -- an event whose fire committed but whose
  ``processed_at`` stamp was lost (re-marked unprocessed) does not double-fire
  on the next tick -- the work_ref dedupe skips the already-fired run.
* **BudgetExceededError** -- a budget-refused fire logs
  ``scheduler_invoke_refused`` once, creates no run, and the drain tick still
  completes.
* **Input-less trigger** -- an event trigger created with no ``inputs`` fires
  with a prompt synthesised from the matched event, wrapped in the
  untrusted-text envelope.

Agent invocation is stubbed through a
:class:`~pydantic_ai.models.function.FunctionModel` so no real LLM is hit; the
Keycloak token + JWT-verify seams ``run_scheduled`` uses are stubbed the same
way :mod:`tests.test_scheduler` stubs them. The tests run on the autouse
SQLite engine from :mod:`tests.conftest`; the real-Postgres end-to-end lives in
:mod:`tests.integration.test_event_matcher_pg`.
"""

from __future__ import annotations

import io
import json
import uuid
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pytest
import structlog
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select

from meho_backplane.agent.invocation import AgentInvoker
from meho_backplane.agent.run import PydanticAgentRun
from meho_backplane.agents.schemas import AgentDefinitionCreate, AgentModelTier
from meho_backplane.agents.service import AgentDefinitionService
from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPrincipal,
    AgentRun,
    AgentRunStatus,
    EventOutbox,
    ScheduledTrigger,
    Tenant,
)
from meho_backplane.events.drain import run_one_drain_tick
from meho_backplane.events.matcher import (
    _event_inputs,
    _payload_contains,
    _synthesize_event_prompt,
    fire_matching_triggers,
)
from meho_backplane.events.outbox import publish
from meho_backplane.operations.agent_run import AGENT_RUN_COMPLETED_EVENT_KIND
from meho_backplane.scheduler.repository import create_event_trigger
from meho_backplane.settings import get_settings
from meho_backplane.untrusted_text import BLOCK_END, BLOCK_START

_TENANT_A = uuid.UUID("11111111-1111-1111-1111-111111111111")
# A sentinel "upstream" agent definition id the seeded events carry. It is
# distinct from the seeded ``reporter`` agent's id, so a subscription that
# targets this upstream (the realistic shape) never re-matches the *fired*
# reporter run's own ``agent_run.completed`` event -- avoiding the
# self-triggering feedback loop a broad ``{"status": "succeeded"}`` filter
# would create against the ``agent_run.completed`` producer.
_UPSTREAM_DEF_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")


@pytest.fixture(autouse=True)
def _required_settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin Settings env vars incl. the seeded agent's client-credentials secret."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    # identity_ref ``agent:reporter`` -> ``AGENT_REPORTER`` env var.
    monkeypatch.setenv("MEHO_AGENT_SECRET_AGENT_REPORTER", "test-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _stub_autonomous_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub ``run_scheduled``'s Keycloak token + JWT-verify seams (offline)."""
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


def _final_text(text: str) -> FunctionModel:
    """A deterministic model that answers immediately with *text*."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(fn)


def _make_invoker() -> AgentInvoker:
    """An invoker over a deterministic FunctionModel (no real LLM)."""
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=lambda: _final_text("done")))


def _exploding_model() -> FunctionModel:
    """A model that fails the test if the loop ever reaches it."""

    def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        pytest.fail("model was invoked; the fire should have been refused before launch")

    return FunctionModel(fn)


def _make_no_call_invoker() -> AgentInvoker:
    """An invoker whose model raises if the loop ever reaches it."""
    return AgentInvoker(runtime=PydanticAgentRun(model_factory=_exploding_model))


async def _seed_tenant_and_agent(name: str = "reporter") -> uuid.UUID:
    """Insert one Tenant + AgentPrincipal + enabled AgentDefinition; return def id.

    Mirrors :func:`tests.test_scheduler._seed_tenant_and_agent`: the principal
    satisfies ``AgentDefinitionService`` identity-ref validation, and the
    definition is what an event trigger's ``agent_definition_id`` points at.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        if await session.get(Tenant, _TENANT_A) is None:
            session.add(Tenant(id=_TENANT_A, slug="tenant-a", name="Tenant A"))
            await session.commit()
        existing = await session.execute(
            select(AgentPrincipal).where(
                AgentPrincipal.tenant_id == _TENANT_A,
                AgentPrincipal.keycloak_client_id == f"agent:{name}",
            )
        )
        if existing.scalar_one_or_none() is None:
            session.add(
                AgentPrincipal(
                    id=uuid.uuid4(),
                    tenant_id=_TENANT_A,
                    name=name,
                    keycloak_client_id=f"agent:{name}",
                    keycloak_internal_id=f"kc-internal-{name}",
                    owner_sub="seed-admin",
                    revoked=False,
                    created_by_sub="seed-admin",
                )
            )
            await session.commit()
    service = AgentDefinitionService()
    entry = await service.create(
        tenant_id=_TENANT_A,
        created_by_sub="seed-admin",
        payload=AgentDefinitionCreate(
            name=name,
            identity_ref=f"agent:{name}",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="You react to events.",
            toolset={},
            turn_budget=2,
            enabled=True,
        ),
    )
    return entry.id


async def _create_event_trigger(
    *,
    agent_definition_id: uuid.UUID,
    event_filter: dict[str, object],
    inputs: dict[str, object] | None = None,
    tenant_id: uuid.UUID = _TENANT_A,
) -> ScheduledTrigger:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await create_event_trigger(
            session,
            tenant_id=tenant_id,
            agent_definition_id=agent_definition_id,
            event_filter=event_filter,
            inputs=inputs,
            identity_sub="__scheduler__",
            created_by_sub="seed-admin",
            in_flight_policy="fail_into_audit",
        )
        await session.commit()
        return row


async def _publish_completed_event(
    *,
    status: str = "succeeded",
    tenant_id: uuid.UUID = _TENANT_A,
    agent_definition_id: uuid.UUID = _UPSTREAM_DEF_ID,
    extra: dict[str, object] | None = None,
) -> int:
    """Publish an ``agent_run.completed`` event; return its ``event_id``."""
    payload: dict[str, object] = {
        "run_id": str(uuid.uuid4()),
        "tenant_id": str(tenant_id),
        "status": status,
        "agent_definition_id": str(agent_definition_id),
        "work_ref": None,
    }
    if extra:
        payload.update(extra)
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await publish(
            session,
            tenant_id=tenant_id,
            event_kind=AGENT_RUN_COMPLETED_EVENT_KIND,
            payload=payload,
        )
        await session.flush()
        event_id = row.event_id
        await session.commit()
    return event_id


async def _all_runs() -> list[AgentRun]:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        return list((await session.execute(select(AgentRun))).scalars().all())


async def _wait_for_runs(expected: int, *, timeout: float = 3.0) -> list[AgentRun]:
    """Poll the ``agent_run`` table until *expected* rows land, or fail."""
    import asyncio

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        runs = await _all_runs()
        if len(runs) >= expected:
            return runs
        await asyncio.sleep(0.05)
    pytest.fail(f"expected {expected} agent_run rows, found {len(await _all_runs())}")


async def _reset_processed(event_id: int) -> None:
    """Re-mark a processed event unprocessed (simulate a lost processed stamp)."""
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(EventOutbox, event_id)
        assert row is not None
        row.processed_at = None
        await session.commit()


async def _is_processed(event_id: int) -> bool:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(EventOutbox, event_id)
        assert row is not None
        return row.processed_at is not None


# ---------------------------------------------------------------------------
# Containment predicate -- direction is load-bearing
# ---------------------------------------------------------------------------


def test_containment_filter_subset_of_payload_matches() -> None:
    """A filter that is a subset of the payload matches (``payload @> filter``)."""
    payload = {"run_id": "r1", "status": "succeeded", "tenant_id": "t1"}
    assert _payload_contains(payload, {"status": "succeeded"})
    assert _payload_contains(payload, {"status": "succeeded", "tenant_id": "t1"})


def test_containment_payload_subset_of_filter_does_not_match() -> None:
    """The reverse direction never matches: payload subset-of filter is False."""
    payload = {"status": "succeeded"}
    # Filter names a key the payload lacks -> not contained.
    assert not _payload_contains(payload, {"status": "succeeded", "tenant_id": "t1"})


def test_containment_value_mismatch_does_not_match() -> None:
    """A present key with a different value is not a match."""
    assert not _payload_contains({"status": "failed"}, {"status": "succeeded"})


def test_containment_empty_filter_matches_everything() -> None:
    """An empty filter names no constraints, so it matches any payload."""
    assert _payload_contains({"status": "succeeded"}, {})
    assert _payload_contains({}, {})


def test_containment_is_recursive_for_nested_objects() -> None:
    """Nested objects use recursive containment (jsonb ``@>`` semantics)."""
    payload = {"meta": {"a": 1, "b": 2}, "status": "ok"}
    assert _payload_contains(payload, {"meta": {"a": 1}})
    assert not _payload_contains(payload, {"meta": {"a": 9}})


def test_containment_array_subset() -> None:
    """Array containment is order-independent subset."""
    assert _payload_contains({"tags": ["x", "y", "z"]}, {"tags": ["y", "x"]})
    assert not _payload_contains({"tags": ["x"]}, {"tags": ["y"]})


# ---------------------------------------------------------------------------
# Prompt synthesis for input-less triggers
# ---------------------------------------------------------------------------


async def test_input_less_trigger_synthesises_untrusted_prompt() -> None:
    """An input-less event trigger renders a prompt wrapping the event, untrusted."""
    def_id = await _seed_tenant_and_agent()
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs=None,
    )
    event = EventOutbox(
        event_id=1,
        tenant_id=_TENANT_A,
        event_kind=AGENT_RUN_COMPLETED_EVENT_KIND,
        payload={"status": "succeeded", "run_id": "r1"},
    )
    prompt = _event_inputs(trigger, event)
    # Untrusted envelope wraps the event body.
    assert BLOCK_START in prompt
    assert BLOCK_END in prompt
    # The event payload is present inside the envelope.
    assert "succeeded" in prompt
    assert AGENT_RUN_COMPLETED_EVENT_KIND in prompt
    # The synthesiser is what produced it (not the empty coerced inputs).
    assert prompt == _event_inputs(trigger, event)
    assert _synthesize_event_prompt(event) in prompt


async def test_trigger_with_prompt_inputs_uses_operator_prompt() -> None:
    """A trigger carrying a usable ``inputs`` prompt uses it, not the synthesiser."""
    def_id = await _seed_tenant_and_agent()
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "investigate the completed run"},
    )
    event = EventOutbox(
        event_id=1,
        tenant_id=_TENANT_A,
        event_kind=AGENT_RUN_COMPLETED_EVENT_KIND,
        payload={"status": "succeeded"},
    )
    assert _event_inputs(trigger, event) == "investigate the completed run"


# ---------------------------------------------------------------------------
# End-to-end: event -> matching trigger -> agent run with work_ref
# ---------------------------------------------------------------------------


async def test_matching_event_fires_run_with_work_ref() -> None:
    """A drained matching event fires an agent run whose work_ref keys the pair."""
    def_id = await _seed_tenant_and_agent()
    # Subscribe to a specific upstream agent (the realistic shape) so the fired
    # run's own completion event does not re-match.
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"agent_definition_id": str(_UPSTREAM_DEF_ID), "status": "succeeded"},
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


async def test_non_matching_filter_fires_nothing() -> None:
    """An event whose payload does not contain the filter fires no run, but drains."""
    def_id = await _seed_tenant_and_agent()
    await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "follow up"},
    )
    # status=failed -> the filter {"status": "succeeded"} is not contained.
    event_id = await _publish_completed_event(status="failed")

    processed = await run_one_drain_tick(invoker=_make_no_call_invoker())
    assert processed == 1
    assert await _is_processed(event_id)
    assert await _all_runs() == []


async def test_input_less_event_trigger_fires_successfully() -> None:
    """An input-less event trigger fires a run that succeeds (prompt was synthesised)."""
    def_id = await _seed_tenant_and_agent()
    await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={},  # empty filter -> matches every event
        inputs=None,
    )
    await _publish_completed_event(status="succeeded")

    processed = await run_one_drain_tick(invoker=_make_invoker())
    assert processed == 1

    runs = await _wait_for_runs(1)
    assert len(runs) == 1
    # The synthesised prompt is a real user turn -> the run is not the typed
    # no-input failure; it runs to success against the deterministic model.
    assert runs[0].status == AgentRunStatus.SUCCEEDED.value


# ---------------------------------------------------------------------------
# Redelivery dedupe -- a re-marked-unprocessed event does not double-fire
# ---------------------------------------------------------------------------


async def test_redelivery_does_not_double_fire() -> None:
    """A claimed-but-unprocessed event re-drained does not fire a second run."""
    def_id = await _seed_tenant_and_agent()
    # Target a specific upstream agent so the *fired* run's own completion event
    # (drained on the second tick) does not itself re-match and fire.
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"agent_definition_id": str(_UPSTREAM_DEF_ID)},
        inputs={"prompt": "follow up"},
    )
    event_id = await _publish_completed_event(status="succeeded")

    # First delivery fires exactly one run.
    assert await run_one_drain_tick(invoker=_make_invoker()) == 1
    runs = await _wait_for_runs(1)
    assert len(runs) == 1
    assert runs[0].work_ref == f"event:{event_id}:{trigger.id}"

    # Simulate a crash between the fire commit and the processed stamp: the
    # event is re-marked unprocessed and re-drained. The exploding invoker
    # proves no fire reaches a model on this tick.
    await _reset_processed(event_id)
    await run_one_drain_tick(invoker=_make_no_call_invoker())

    # No second run: the work_ref dedupe found the first delivery's run for the
    # redelivered event, and the fired run's own completion event (also pending)
    # does not match the upstream-scoped filter. The count is the invariant --
    # the number of events processed on this tick races background publishing.
    assert len(await _all_runs()) == 1


# ---------------------------------------------------------------------------
# BudgetExceededError -- no retry, one structured log, tick completes
# ---------------------------------------------------------------------------


def _capture_loop_log_to_buffer(monkeypatch: pytest.MonkeyPatch) -> io.StringIO:
    """Redirect the scheduler-loop logger to a buffer (fire-refusal logs land here).

    The matcher reuses ``scheduler.loop._dispatch_invocation``, which logs the
    budget refusal via that module's ``_log``. Mirrors
    :func:`tests.test_scheduler._capture_structlog_to_buffer`: rebind the
    module-level proxy so emissions bypass the cached BoundLogger.
    """
    import logging as _stdlib_logging

    from meho_backplane.scheduler import loop as _loop_module

    buf = io.StringIO()
    structlog.reset_defaults()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_stdlib_logging.INFO),
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    monkeypatch.setattr(_loop_module, "_log", structlog.get_logger(_loop_module.__name__))
    return buf


def _invoke_refused_lines(buf: io.StringIO) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for line in buf.getvalue().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("event") == "scheduler_invoke_refused":
            out.append(entry)
    return out


async def test_budget_refused_fire_does_not_retry_and_logs_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A budget-refused subscription fire creates no run, logs once, tick completes."""
    def_id = await _seed_tenant_and_agent()
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "follow up"},
    )
    event_id = await _publish_completed_event(status="succeeded")

    monkeypatch.setenv("AGENT_RUNS_DISABLED_GLOBAL", "true")
    get_settings.cache_clear()

    buf = _capture_loop_log_to_buffer(monkeypatch)
    try:
        # The model must never be reached -- the budget gate refuses before
        # the run row is even created.
        processed = await run_one_drain_tick(invoker=_make_no_call_invoker())
    finally:
        structlog.reset_defaults()

    # The event is still consumed and the tick completed.
    assert processed == 1
    assert await _is_processed(event_id)
    # No run row -- the refusal short-circuits before persistence.
    assert await _all_runs() == []
    # Exactly one structured refusal log carrying the budget tag.
    refused = [
        line for line in _invoke_refused_lines(buf) if line.get("reason") == "BudgetExceededError"
    ]
    assert len(refused) == 1, buf.getvalue()
    assert refused[0]["trigger_id"] == str(trigger.id)
    assert "global kill switch" in refused[0]["budget_reason"]


# ---------------------------------------------------------------------------
# Tenant + status scoping
# ---------------------------------------------------------------------------


async def test_paused_event_trigger_does_not_fire() -> None:
    """A non-active (paused) event trigger is not fired."""
    def_id = await _seed_tenant_and_agent()
    trigger = await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "follow up"},
    )
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = await session.get(ScheduledTrigger, trigger.id)
        assert row is not None
        row.status = "paused"
        await session.commit()

    await _publish_completed_event(status="succeeded")
    assert await run_one_drain_tick(invoker=_make_no_call_invoker()) == 1
    assert await _all_runs() == []


async def test_fire_matching_triggers_returns_fired_count() -> None:
    """The public entry returns the number of triggers it fired."""
    def_id = await _seed_tenant_and_agent()
    await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={"status": "succeeded"},
        inputs={"prompt": "one"},
    )
    await _create_event_trigger(
        agent_definition_id=def_id,
        event_filter={},  # also matches
        inputs={"prompt": "two"},
    )
    event_id = await _publish_completed_event(status="succeeded")
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        event = await session.get(EventOutbox, event_id)
        assert event is not None
        fired = await fire_matching_triggers(event, _make_invoker())
    assert fired == 2
    await _wait_for_runs(2)
