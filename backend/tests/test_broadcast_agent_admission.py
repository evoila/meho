# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Trusted admission and claim rules for intentional announcements (#3736).

Covers the task's acceptance criteria with the real admission check and
the real database (the test SQLite schema has migration ``0108``
applied). Only the Valkey ``XADD`` and the rate limiter are faked.

* Forged client fields (``run_id``, a person's token, an OAuth client id
  on a person's token, an unregistered or revoked agent) never pass.
* A live hosted run and a registered agent principal both publish, with
  the identity stamped by the server.
* A person's token gets ``agent_identity_required`` plus the remediation.
* ``start`` / ``update`` / ``completion`` enforce owner, claim UUID,
  default TTL, and per-tenant, per-owner idempotency.
* A legacy archive row (written before trusted admission) can never be
  continued as a claim.
* A step whose stream write failed is delivered by the retry with the
  same key, once, without a second row.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.agent.invoke import current_agent_run_id_var
from meho_backplane.auth.agent_admission import (
    AGENT_IDENTITY_REQUIRED,
    AgentIdentityRequiredError,
    resolve_agent_admission,
)
from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.broadcast.history import row_to_announcement
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AgentAnnouncement
from meho_backplane.mcp.schemas import INVALID_PARAMS
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.mcp.tools.broadcast import _handler_announce
from tests.broadcast_admission_helpers import (
    FakeStreamClient,
    seed_agent_principal,
    seed_agent_run,
)
from tests.mcp_test_fixtures import (
    client_with_operator,  # noqa: F401 -- pytest-discovered fixture
    isolated_registry,  # noqa: F401 -- pytest-discovered autouse fixture
    post_mcp,
    required_settings_env,  # noqa: F401 -- pytest-discovered autouse fixture
)

_AGENT_CLIENT = "agent:triage"


@pytest.fixture(autouse=True)
def _fake_valkey(
    monkeypatch: pytest.MonkeyPatch,
    isolated_registry: None,  # noqa: F811 -- reload the tool module first
) -> FakeStreamClient:
    """Fake the Valkey client the publisher writes to and switch the rate limiter off."""
    client = FakeStreamClient()
    monkeypatch.setattr("meho_backplane.broadcast.publisher.get_broadcast_client", lambda: client)
    for target in (
        "meho_backplane.broadcast.enforce_announce_rate_limit",
        "meho_backplane.mcp.tools.broadcast.enforce_announce_rate_limit",
    ):
        monkeypatch.setattr(target, AsyncMock(return_value=None))
    return client


@pytest.fixture
def _fake_stream(_fake_valkey: FakeStreamClient) -> AsyncMock:
    """The fake ``XADD``, for tests that assert on stream writes."""
    return _fake_valkey.xadd


def _operator(
    tenant_id: UUID,
    *,
    sub: str = "svc-account-1",
    kind: PrincipalKind = PrincipalKind.AGENT,
    client_id: str | None = _AGENT_CLIENT,
) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="x",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
        client_id=client_id,
    )


def _human(tenant_id: UUID, sub: str = "person-1") -> Operator:
    return _operator(tenant_id, sub=sub, kind=PrincipalKind.USER, client_id=None)


async def _registered_agent(tenant_id: UUID, client_id: str = _AGENT_CLIENT) -> Operator:
    await seed_agent_principal(tenant_id, client_id)
    return _operator(tenant_id, sub=f"svc-{client_id}", client_id=client_id)


async def _announce_error(operator: Operator, arguments: dict[str, Any]) -> dict[str, Any]:
    with pytest.raises(McpInvalidParamsError) as excinfo:
        await _handler_announce(operator, arguments)
    assert excinfo.value.data is not None
    return excinfo.value.data


async def _row_count(tenant_id: UUID) -> int:
    async with get_sessionmaker()() as session:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(AgentAnnouncement)
                    .where(AgentAnnouncement.tenant_id == tenant_id)
                )
            ).scalar_one()
        )


class _AmbientRun:
    """Bind the hosted-run context the agent runtime sets around a loop."""

    def __init__(self, run_id: UUID) -> None:
        self._run_id = run_id

    def __enter__(self) -> None:
        self._token = current_agent_run_id_var.set(self._run_id)

    def __exit__(self, *_exc: object) -> None:
        current_agent_run_id_var.reset(self._token)


# ---------------------------------------------------------------------------
# AC1: forged client agent / run fields never satisfy admission
# ---------------------------------------------------------------------------


async def test_person_with_forged_run_id_is_refused() -> None:
    """A ``run_id`` argument is never identity proof."""
    tenant_id = uuid4()
    run_id = await seed_agent_run(tenant_id, identity_sub="person-1")
    data = await _announce_error(_human(tenant_id), {"activity": "x", "run_id": str(run_id)})
    assert data["code"] == AGENT_IDENTITY_REQUIRED
    assert await _row_count(tenant_id) == 0


@pytest.mark.parametrize(
    ("kind", "client_id"),
    [
        (PrincipalKind.USER, _AGENT_CLIENT),  # a client id on a person's token
        (PrincipalKind.SERVICE, _AGENT_CLIENT),  # a service account, not an agent
        (PrincipalKind.RUNNER, _AGENT_CLIENT),
    ],
)
async def test_non_agent_token_is_refused_even_with_agent_client_id(
    kind: PrincipalKind, client_id: str
) -> None:
    tenant_id = uuid4()
    await seed_agent_principal(tenant_id, client_id)
    with pytest.raises(AgentIdentityRequiredError):
        await resolve_agent_admission(_operator(tenant_id, kind=kind, client_id=client_id))


async def test_unregistered_agent_is_refused() -> None:
    tenant_id = uuid4()
    with pytest.raises(AgentIdentityRequiredError, match="not registered"):
        await resolve_agent_admission(_operator(tenant_id))


async def test_agent_registered_in_another_tenant_is_refused() -> None:
    await seed_agent_principal(uuid4(), _AGENT_CLIENT)
    with pytest.raises(AgentIdentityRequiredError, match="not registered"):
        await resolve_agent_admission(_operator(uuid4()))


async def test_revoked_agent_is_refused() -> None:
    tenant_id = uuid4()
    await seed_agent_principal(tenant_id, _AGENT_CLIENT, revoked=True)
    with pytest.raises(AgentIdentityRequiredError, match="revoked"):
        await resolve_agent_admission(_operator(tenant_id))


async def test_ambient_run_from_another_tenant_is_refused() -> None:
    run_id = await seed_agent_run(uuid4(), identity_sub="person-1")
    with _AmbientRun(run_id), pytest.raises(AgentIdentityRequiredError, match="does not exist"):
        await resolve_agent_admission(_human(uuid4()))


async def test_finished_ambient_run_is_refused() -> None:
    tenant_id = uuid4()
    run_id = await seed_agent_run(tenant_id, identity_sub="person-1", status="succeeded")
    with _AmbientRun(run_id), pytest.raises(AgentIdentityRequiredError, match="succeeded"):
        await resolve_agent_admission(_human(tenant_id))


# ---------------------------------------------------------------------------
# AC2: hosted run and registered agent publish with stamped identity
# ---------------------------------------------------------------------------


async def test_hosted_run_publishes_with_stamped_identity(_fake_stream: AsyncMock) -> None:
    tenant_id = uuid4()
    person = _human(tenant_id)
    run_id = await seed_agent_run(tenant_id, identity_sub=person.sub, work_ref="gh:org/repo#7")
    with _AmbientRun(run_id), actor_delegation(_AGENT_CLIENT):
        ack = await _handler_announce(person, {"activity": "patching hosts"})

    assert ack["admission"] == "hosted_run"
    assert ack["owner_sub"] == _AGENT_CLIENT
    assert ack["actor_sub"] == _AGENT_CLIENT
    assert ack["run_id"] == str(run_id)
    assert ack["work_ref"] == "gh:org/repo#7"
    async with get_sessionmaker()() as session:
        row = await session.get(AgentAnnouncement, UUID(ack["event_id"]))
    assert row is not None
    assert row.principal_sub == person.sub
    assert row.owner_sub == _AGENT_CLIENT
    assert row.actor_sub == _AGENT_CLIENT
    assert row.run_id == run_id
    assert row.work_ref == "gh:org/repo#7"
    assert row.admission == "hosted_run"
    assert row.claim_id == UUID(ack["claim_id"])
    _fake_stream.assert_awaited_once()


async def test_hosted_run_refuses_a_different_work_ref() -> None:
    tenant_id = uuid4()
    person = _human(tenant_id)
    run_id = await seed_agent_run(tenant_id, identity_sub=person.sub, work_ref="gh:org/repo#7")
    with _AmbientRun(run_id):
        data = await _announce_error(person, {"activity": "x", "work_ref": "gh:org/repo#8"})
    assert data["code"] == "server_stamped_field"


async def test_registered_agent_publishes_with_stamped_identity() -> None:
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    ack = await _handler_announce(agent, {"activity": "draining node-3", "work_ref": "CR-12"})

    assert ack["admission"] == "agent_principal"
    assert ack["owner_sub"] == _AGENT_CLIENT
    assert ack["work_ref"] == "CR-12"
    assert "run_id" not in ack
    assert "actor_sub" not in ack


async def test_registered_agent_cannot_claim_a_run_id() -> None:
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    data = await _announce_error(agent, {"activity": "x", "run_id": str(uuid4())})
    assert data["code"] == "server_stamped_field"
    assert await _row_count(tenant_id) == 0


# ---------------------------------------------------------------------------
# AC3: a person's interactive publish returns agent_identity_required
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("client_with_operator", [TenantRole.OPERATOR], indirect=True)
def test_person_on_the_mcp_wire_gets_agent_identity_required(
    client_with_operator: tuple[TestClient, Operator],  # noqa: F811
) -> None:
    client, _op = client_with_operator
    resp = post_mcp(
        client,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "meho_broadcast_announce", "arguments": {"activity": "hello"}},
        },
    )
    error = resp.json()["error"]
    assert error["code"] == INVALID_PARAMS
    assert error["data"]["code"] == AGENT_IDENTITY_REQUIRED
    assert "meho agent-principal register" in error["data"]["remediation"]
    assert "hosted agent run" in error["data"]["remediation"]


# ---------------------------------------------------------------------------
# AC4: owner, claim UUID, default TTL and tenant / publisher idempotency
# ---------------------------------------------------------------------------


async def test_start_mints_claim_id_and_default_ttl() -> None:
    agent = await _registered_agent(uuid4())
    first = await _handler_announce(agent, {"activity": "a"})
    second = await _handler_announce(agent, {"activity": "b", "ttl_minutes": 5})
    assert first["claim_id"] != second["claim_id"]
    assert first["ttl_minutes"] == 30
    assert second["ttl_minutes"] == 5


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"phase": "start", "claim_id": str(uuid4())}, "claim_id_not_allowed"),
        ({"phase": "update", "idempotency_key": "k1"}, "claim_id_required"),
        ({"phase": "completion", "claim_id": str(uuid4())}, "idempotency_key_required"),
    ],
)
async def test_claim_step_shape_is_checked(arguments: dict[str, Any], code: str) -> None:
    agent = await _registered_agent(uuid4())
    data = await _announce_error(agent, {"activity": "x", **arguments})
    assert data["code"] == code


async def test_update_and_completion_continue_the_claim(_fake_stream: AsyncMock) -> None:
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    start = await _handler_announce(agent, {"activity": "start"})
    claim = start["claim_id"]
    update = await _handler_announce(
        agent,
        {"activity": "half done", "phase": "update", "claim_id": claim, "idempotency_key": "u1"},
    )
    done = await _handler_announce(
        agent,
        {"activity": "done", "phase": "completion", "claim_id": claim, "idempotency_key": "c1"},
    )
    assert update["claim_id"] == done["claim_id"] == claim
    assert await _row_count(tenant_id) == 3
    assert _fake_stream.await_count == 3

    data = await _announce_error(
        agent, {"activity": "more", "phase": "update", "claim_id": claim, "idempotency_key": "u2"}
    )
    assert data["code"] == "claim_completed"
    data = await _announce_error(
        agent,
        {"activity": "again", "phase": "completion", "claim_id": claim, "idempotency_key": "c2"},
    )
    assert data["code"] == "claim_completed"


async def test_only_the_owner_can_update() -> None:
    tenant_id = uuid4()
    owner = await _registered_agent(tenant_id)
    other = await _registered_agent(tenant_id, "agent:other")
    start = await _handler_announce(owner, {"activity": "mine"})
    data = await _announce_error(
        other,
        {
            "activity": "steal",
            "phase": "update",
            "claim_id": start["claim_id"],
            "idempotency_key": "k",
        },
    )
    assert data["code"] == "claim_not_owned"


async def test_hosted_claim_is_bound_to_its_run() -> None:
    """Another run of the same agent cannot continue the claim."""
    tenant_id = uuid4()
    person = _human(tenant_id)
    run_a = await seed_agent_run(tenant_id, identity_sub=person.sub)
    run_b = await seed_agent_run(tenant_id, identity_sub=person.sub)
    with _AmbientRun(run_a), actor_delegation(_AGENT_CLIENT):
        start = await _handler_announce(person, {"activity": "run a"})
    with _AmbientRun(run_b), actor_delegation(_AGENT_CLIENT):
        data = await _announce_error(
            person,
            {
                "activity": "run b",
                "phase": "update",
                "claim_id": start["claim_id"],
                "idempotency_key": "k",
            },
        )
    assert data["code"] == "claim_not_owned"


async def test_claim_from_another_tenant_is_not_found() -> None:
    owner = await _registered_agent(uuid4())
    start = await _handler_announce(owner, {"activity": "tenant a"})
    # Client ids are unique across tenants, so the stranger is another agent.
    # A same-tenant stranger would get claim_not_owned; another tenant sees
    # no claim at all.
    stranger = await _registered_agent(uuid4(), "agent:stranger")
    data = await _announce_error(
        stranger,
        {"activity": "x", "phase": "update", "claim_id": start["claim_id"], "idempotency_key": "k"},
    )
    assert data["code"] == "claim_not_found"


async def test_same_key_replays_without_a_second_write(_fake_stream: AsyncMock) -> None:
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    start = await _handler_announce(agent, {"activity": "start"})
    step = {
        "activity": "progress",
        "phase": "update",
        "claim_id": start["claim_id"],
        "idempotency_key": "u1",
    }
    first = await _handler_announce(agent, step)
    again = await _handler_announce(agent, step)

    assert first["replayed"] is False
    assert again["replayed"] is True
    assert again["event_id"] == first["event_id"]
    assert again["cursor"] is None
    assert await _row_count(tenant_id) == 2
    assert _fake_stream.await_count == 2

    data = await _announce_error(agent, {**step, "activity": "something else"})
    assert data["code"] == "idempotency_key_reused"


async def test_retry_finishes_a_delivery_whose_stream_write_failed(
    _fake_stream: AsyncMock,
) -> None:
    """The row is stored, the stream write fails, the retry delivers it once."""
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    start = await _handler_announce(agent, {"activity": "start"})
    step = {
        "activity": "done",
        "phase": "completion",
        "claim_id": start["claim_id"],
        "idempotency_key": "c1",
    }
    outcomes: list[Any] = [RedisError("valkey down"), "1700000000000-77"]

    def _next_outcome(*_args: Any, **_kwargs: Any) -> str:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return str(outcome)

    _fake_stream.side_effect = _next_outcome

    with pytest.raises(RedisError):
        await _handler_announce(agent, step)
    assert await _row_count(tenant_id) == 2
    async with get_sessionmaker()() as session:
        pending = (
            await session.execute(
                select(AgentAnnouncement).where(AgentAnnouncement.phase == "completion")
            )
        ).scalar_one()
        assert pending.stream_entry_id is None
        pending_id = pending.id

    delivered = await _handler_announce(agent, step)
    assert delivered["replayed"] is True
    assert delivered["cursor"] == "1700000000000-77"
    assert delivered["event_id"] == str(pending_id)
    assert await _row_count(tenant_id) == 2
    assert _fake_stream.await_count == 3
    _stream_key, fields = _fake_stream.await_args.args
    assert str(pending_id) in fields["event"]
    async with get_sessionmaker()() as session:
        marked = await session.get(AgentAnnouncement, pending_id)
        assert marked is not None
        assert marked.stream_entry_id == "1700000000000-77"

    again = await _handler_announce(agent, step)
    assert again["replayed"] is True
    assert again["cursor"] is None
    assert _fake_stream.await_count == 3

    data = await _announce_error(
        agent,
        {
            "activity": "late",
            "phase": "update",
            "claim_id": start["claim_id"],
            "idempotency_key": "u9",
        },
    )
    assert data["code"] == "claim_completed"


async def _completion_step(agent: Operator, key: str) -> tuple[dict[str, Any], UUID]:
    """Open a claim and return a completion step for it plus the start's claim id."""
    start = await _handler_announce(agent, {"activity": "start"})
    step = {
        "activity": "done",
        "phase": "completion",
        "claim_id": start["claim_id"],
        "idempotency_key": key,
    }
    return step, UUID(start["claim_id"])


async def _completion_row(claim_id: UUID) -> AgentAnnouncement:
    async with get_sessionmaker()() as session:
        return (
            await session.execute(
                select(AgentAnnouncement).where(
                    AgentAnnouncement.claim_id == claim_id,
                    AgentAnnouncement.phase == "completion",
                )
            )
        ).scalar_one()


async def test_retry_after_a_failed_delivery_mark_writes_no_second_entry(
    _fake_valkey: FakeStreamClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stream write lands, the delivery mark does not commit, the retry writes nothing."""
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    step, claim_id = await _completion_step(agent, "c1")

    original_commit: Callable[[AsyncSession], Awaitable[None]] = AsyncSession.commit
    fail_next_commit = False

    async def _xadd_then_fail_mark(*args: Any, **kwargs: Any) -> str:
        nonlocal fail_next_commit
        fail_next_commit = True
        return "1700000000000-77"

    async def _commit(session: AsyncSession) -> None:
        nonlocal fail_next_commit
        if fail_next_commit:
            fail_next_commit = False
            raise OperationalError("UPDATE agent_announcement", {}, Exception("connection lost"))
        await original_commit(session)

    _fake_valkey.xadd.side_effect = _xadd_then_fail_mark
    monkeypatch.setattr(AsyncSession, "commit", _commit)

    first = await _handler_announce(agent, step)
    assert first["replayed"] is False
    assert first["cursor"] == "1700000000000-77"
    assert (await _completion_row(claim_id)).stream_entry_id is None

    retry = await _handler_announce(agent, step)
    assert retry["replayed"] is True
    assert retry["cursor"] is None
    assert retry["event_id"] == first["event_id"]
    assert _fake_valkey.xadd.await_count == 2, "start + one completion write, no duplicate"
    assert (await _completion_row(claim_id)).stream_entry_id == "1700000000000-77"
    assert await _row_count(tenant_id) == 2


async def test_retry_after_a_lost_stream_reply_writes_no_second_entry(
    _fake_valkey: FakeStreamClient,
) -> None:
    """The stream write lands on the server, its reply is lost, the retry writes nothing."""
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    step, claim_id = await _completion_step(agent, "c1")

    _fake_valkey.lose_next_response = True
    with pytest.raises(RedisTimeoutError):
        await _handler_announce(agent, step)
    assert (await _completion_row(claim_id)).stream_entry_id is None
    assert _fake_valkey.xadd.await_count == 2
    _stream_key, fields = _fake_valkey.xadd.await_args.args
    written_event_id = json.loads(fields["event"])["event_id"]

    retry = await _handler_announce(agent, step)
    assert retry["replayed"] is True
    assert retry["cursor"] is None
    assert retry["event_id"] == written_event_id
    assert _fake_valkey.xadd.await_count == 2, "the retry found the entry and wrote nothing"
    assert (await _completion_row(claim_id)).stream_entry_id == "1700000000000-2"
    assert await _row_count(tenant_id) == 2

    again = await _handler_announce(agent, step)
    assert again["replayed"] is True
    assert again["cursor"] is None
    assert _fake_valkey.xadd.await_count == 2


async def test_same_key_from_another_run_is_refused() -> None:
    """Two hosted runs of one agent share an owner, but not a replay."""
    tenant_id = uuid4()
    person = _human(tenant_id)
    run_a = await seed_agent_run(tenant_id, identity_sub=person.sub)
    run_b = await seed_agent_run(tenant_id, identity_sub=person.sub)
    step = {"activity": "same work", "idempotency_key": "start-1"}
    with _AmbientRun(run_a), actor_delegation(_AGENT_CLIENT):
        await _handler_announce(person, step)
        data = await _announce_error(person, {**step, "scope": "other scope"})
    assert data["code"] == "idempotency_key_reused"
    with _AmbientRun(run_b), actor_delegation(_AGENT_CLIENT):
        data = await _announce_error(person, step)
    assert data["code"] == "idempotency_key_reused"
    assert await _row_count(tenant_id) == 1


async def test_idempotency_key_is_scoped_per_owner() -> None:
    tenant_id = uuid4()
    first = await _registered_agent(tenant_id)
    second = await _registered_agent(tenant_id, "agent:other")
    a = await _handler_announce(first, {"activity": "a", "idempotency_key": "shared"})
    b = await _handler_announce(second, {"activity": "b", "idempotency_key": "shared"})
    assert a["replayed"] is False
    assert b["replayed"] is False
    assert a["claim_id"] != b["claim_id"]


# ---------------------------------------------------------------------------
# AC5: a legacy archive row cannot become an active claim
# ---------------------------------------------------------------------------


async def test_legacy_archive_row_cannot_be_continued() -> None:
    tenant_id = uuid4()
    agent = await _registered_agent(tenant_id)
    legacy_id = uuid4()
    async with get_sessionmaker()() as session:
        session.add(
            AgentAnnouncement(
                id=legacy_id,
                tenant_id=tenant_id,
                principal_sub=agent.sub,
                activity="old announcement",
                targets=[],
                phase="start",
                created_at=datetime.now(UTC),
            )
        )
        await session.commit()
        legacy = await session.get(AgentAnnouncement, legacy_id)
        assert legacy is not None
        assert row_to_announcement(legacy).is_trusted_claim is False

    data = await _announce_error(
        agent,
        {
            "activity": "revive",
            "phase": "update",
            "claim_id": str(legacy_id),
            "idempotency_key": "k",
        },
    )
    assert data["code"] == "claim_not_found"
    data = await _announce_error(
        agent,
        {
            "activity": "close",
            "phase": "completion",
            "claim_id": str(legacy_id),
            "idempotency_key": "k2",
        },
    )
    assert data["code"] == "claim_not_found"
