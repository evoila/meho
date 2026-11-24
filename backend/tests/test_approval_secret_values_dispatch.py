# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret values of a waiting call, through the real dispatcher.

Each test dispatches a typed write or a multi-step operation that waits for
a person, against the migrated SQLite engine. Covered:

* the stored card and the approval view show no secret value: neither a
  value of a field the connector marked, nor a password sent as a number;
* a connector that sends the saved note back: the stored result and the
  audit row of the run show the marker, and the run sent the real values;
* the AI agent that waited runs the params from the one-time store, checked
  against the approved hash, not its copy in memory;
* a step that takes a plain id, under a parent that takes a secret: the row
  keeps the parent only in the one-time store, and the approved parent runs;
* without a valid key, a step gets the refusal a direct call gets;
* a number of days under a password-like name stays as it is.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane import op_classes
from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.api.v1.approvals import _proposed_effect_for
from meho_backplane.auth.operator import Operator, PrincipalKind
from meho_backplane.broadcast import reset_broadcast_client_for_testing
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._preview import proposed_effect_for_agent
from meho_backplane.operations._validate import compute_params_hash
from meho_backplane.operations.approval_queue import (
    create_pending_request,
    params_with_secret_fields_hidden,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.dispatcher import result_handoff_key_missing
from meho_backplane.settings import get_settings
from tests._people_behind_helpers import give_agent_a_known_person

from . import _approval_secret_dispatch_helpers as d
from . import _composite_wait_audit_helpers as h

pytestmark = pytest.mark.asyncio

_SECRET = "made-up-note-value-4711"
_PIN = 47114711
_MARKER = "[REDACTED:param_name]"
_PARAMS: dict[str, Any] = {"name": "web", "note": f"keep {_SECRET}", "password": _PIN}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("BROADCAST_REDIS_URL", "redis://broadcast.test:6379")
    monkeypatch.setenv("AGENT_APPROVAL_WAIT_TIMEOUT_SECONDS", "2.0")
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()
    reset_dispatcher_caches()
    clear_registry()
    d.received.clear()
    op_classes.declare_secret_field(d.NOTE_OP_ID, "note")
    yield
    op_classes._SECRET_FIELDS.pop(d.NOTE_OP_ID, None)
    op_classes._SECRET_FIELDS.pop(d.PARENT_OP_ID, None)
    d.received.clear()
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()
    reset_broadcast_client_for_testing()


@pytest.fixture
async def ops() -> AsyncIterator[AsyncSession]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = h.EMBEDDING
    embedding.encode.return_value = [h.EMBEDDING]
    embedding.dimension = 384
    await h.register_child(embedding)
    await d.register_note_op(embedding)
    async with get_sessionmaker()() as session:
        await h.register_composite(
            session,
            d.PARENT_OP_ID,
            "parent_with_secret_composite",
            module="tests._approval_secret_dispatch_helpers",
        )
        yield session


async def _call(op_id: str, params: dict[str, Any], who: Operator | None = None) -> OperationResult:
    return await dispatch(
        operator=who or h.operator(),
        connector_id=h.CONNECTOR_ID,
        op_id=op_id,
        target=None,
        params=params,
    )


async def _everything_stored() -> str:
    """Every approval request row and every audit row, as one text."""
    async with get_sessionmaker()() as s:
        rows: list[Any] = list((await s.execute(select(ApprovalRequest))).scalars())
        rows += list((await s.execute(select(AuditLog))).scalars())
    return "\n".join(
        json.dumps({c.name: str(getattr(row, c.name)) for c in type(row).__table__.columns})
        for row in rows
    )


def _holds_a_secret(text: str) -> bool:
    return _SECRET in text or str(_PIN) in text


async def test_the_card_of_a_waiting_call_shows_no_secret_value(ops: AsyncSession) -> None:
    result = await _call(d.NOTE_OP_ID, _PARAMS)

    assert result.status == "awaiting_approval", result.error
    row = await h.request(UUID(result.extras["approval_request_id"]))
    assert row.execution_handle is not None
    assert row.params == {"name": "web", "note": _MARKER, "password": _MARKER}
    card = json.dumps(row.proposed_effect)
    assert "web" in card
    assert not _holds_a_secret(card)
    assert not _holds_a_secret(result.model_dump_json())
    view = _proposed_effect_for(h.operator(sub="op-approver"), row)
    assert not _holds_a_secret(json.dumps(view))
    assert not _holds_a_secret(await _everything_stored())


async def test_a_note_the_connector_sends_back_is_hidden_in_the_result_and_the_audit_row(
    ops: AsyncSession,
) -> None:
    waiting = await _call(d.NOTE_OP_ID, _PARAMS)
    request = await h.approve(UUID(waiting.extras["approval_request_id"]))

    result = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )

    assert result.status == "ok", result.error
    assert d.received == [(d.NOTE_OP_ID, _PARAMS)]
    assert result.result == {"saved": {"note": _MARKER, "pin": _MARKER}}
    stored = await _everything_stored()
    assert "resume_result" in stored
    assert not _holds_a_secret(stored)


async def test_the_agent_that_waited_runs_the_approved_params_not_its_copy(
    ops: AsyncSession,
) -> None:
    agent = h.operator(sub="agent-requester", principal_kind=PrincipalKind.AGENT)
    await give_agent_a_known_person(h.TENANT_ID, "agent-requester")
    waiting = await _call(d.NOTE_OP_ID, _PARAMS, agent)
    assert waiting.status == "awaiting_approval", waiting.error
    await h.approve(UUID(waiting.extras["approval_request_id"]))

    resumed = await resume_or_surface_awaiting_approval(
        operator=agent,
        call_arguments={
            "connector_id": h.CONNECTOR_ID,
            "op_id": d.NOTE_OP_ID,
            "params": {**_PARAMS, "note": "another note"},
            "target": None,
        },
        awaiting_envelope=waiting.model_dump(mode="json"),
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "ok", resumed
    assert d.received == [(d.NOTE_OP_ID, _PARAMS)]
    assert not _holds_a_secret(await _everything_stored())


@pytest.mark.parametrize("marked", [False, True], ids=["secret_name", "marked_field"])
async def test_a_parent_with_a_secret_is_kept_only_in_the_one_time_store(
    ops: AsyncSession, marked: bool
) -> None:
    if marked:
        op_classes.declare_secret_field(d.PARENT_OP_ID, "note")
        params: dict[str, Any] = {"vm": "vm-1", "note": _SECRET}
    else:
        params = {"vm": "vm-1", "admin_password": _SECRET}

    waiting = await _call(d.PARENT_OP_ID, params)

    assert waiting.status == "awaiting_approval", waiting.error
    row = await h.request(UUID(waiting.extras["approval_request_id"]))
    assert row.op_id == h.STEP_A
    assert row.params == {"name": "vm-1"}
    assert row.resume_parent == {"op_id": d.PARENT_OP_ID}
    assert row.execution_handle is not None
    assert _SECRET not in await _everything_stored()

    approved = await h.approve(row.id)
    result = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=approved, params=None
    )

    assert result.status == "ok", result.error
    assert d.received == [(d.PARENT_OP_ID, params), (d.PARENT_OP_ID, params)]
    assert _SECRET not in await _everything_stored()


async def test_without_the_key_a_step_gets_the_refusal_of_a_direct_call(
    ops: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APPROVAL_HANDOFF_ENCRYPTION_KEY", "")
    get_settings.cache_clear()

    direct = await _call(d.NOTE_OP_ID, _PARAMS)
    step = await _call(d.PARENT_OP_ID, {"vm": "vm-1", "admin_password": _SECRET})

    expected = result_handoff_key_missing(d.PARENT_OP_ID, 0.0).error
    assert direct.status == "denied"
    assert direct.error == expected
    assert step.status == "denied", step.error
    assert step.error == expected
    assert "connector" not in (step.extras.get("error_code") or "")
    async with get_sessionmaker()() as s:
        assert list((await s.execute(select(ApprovalRequest))).scalars()) == []


async def test_a_number_of_days_under_a_password_name_stays(ops: AsyncSession) -> None:
    params = {"name": "web", "extras": {"password_max_age_days": 90}}

    assert params_with_secret_fields_hidden(params, op_id="targets.register") is None
    waiting = await _call(d.NOTE_OP_ID, params)

    row = await h.request(UUID(waiting.extras["approval_request_id"]))
    assert row.params == params
    assert row.execution_handle is None
    assert row.proposed_effect is not None
    assert row.proposed_effect["params_echo"]["extras"] == {"password_max_age_days": 90}


async def test_the_agent_view_of_a_register_card_keeps_the_number_of_days() -> None:
    import meho_backplane.connectors.targets  # noqa: F401  (registers the card switch)

    params = {"name": "web", "host": "web.example.test", "extras": {"password_max_age_days": 90}}
    async with get_sessionmaker()() as s:
        request = await create_pending_request(
            s,
            operator=h.operator(),
            connector_id="targets",
            op_id="targets.register",
            target=None,
            params=params,
            params_hash=compute_params_hash(params),
            proposed_effect={"op_id": "targets.register", "preview": {"name": "web"}},
        )
        await s.commit()

    shown = proposed_effect_for_agent(
        request.op_id,
        request.proposed_effect,
        params=request.params,
        connector_id=request.connector_id,
        tenant=str(request.tenant_id),
    )

    assert shown is not None
    assert shown["params_echo"]["extras"] == {"password_max_age_days": 90}
