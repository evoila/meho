# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The secret values of an approved call, in what the connector sends back.

Each test runs a typed write that waited for a person, through the real
dispatcher, against the migrated SQLite engine. Covered:

* a marked note that is also a word MEHO uses (``uncertain``) leaves MEHO's
  own fields of the result as they are: a write that timed out stays
  uncertain, and a second run is refused;
* a long marked note in an HTTP 422 error is hidden before MEHO shortens
  the error text, so no part of it is stored;
* the text of an SNMP ``community`` pair is hidden where the connector
  sends it back under another name;
* a connector that denies the call with a reason that quotes the note;
* license keys sent back as the keys of an object: each key is hidden, and
  the two entries stay apart;
* a step of a multi-step operation whose stored params were changed does
  not run for the AI agent that waited;
* the matcher keeps no copy of a secret in the shared cache of ``re``.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane import op_classes
from meho_backplane.agent.approval_wait import resume_or_surface_awaiting_approval
from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.approval_rerun import RerunNotAllowedError, rerun_failed_request
from meho_backplane.operations.approval_secret_values import (
    CallSecretValues,
    hide_call_secret_values,
)
from tests._people_behind_helpers import give_agent_a_known_person
from tests.test_approval_secret_values_dispatch import (
    _call,
    _env,
    _everything_stored,
    _handoff_rows,
)

from . import _approval_secret_dispatch_helpers as d
from . import _composite_wait_audit_helpers as h

# The fixture of the dispatch module, so this module sets up the same way.
__all__ = ["_env"]

pytestmark = pytest.mark.asyncio

_MARKER = "[REDACTED:param_name]"


@pytest.fixture
async def ops() -> AsyncIterator[AsyncSession]:
    embedding = AsyncMock()
    embedding.encode_one.return_value = h.EMBEDDING
    embedding.encode.return_value = [h.EMBEDDING]
    embedding.dimension = 384
    await h.register_child(embedding)
    await d.register_echo_ops(embedding)
    await d.register_more_note_ops(embedding)
    for op_id in (d.TIMEOUT_OP_ID, d.DENY_OP_ID):
        op_classes.declare_secret_field(op_id, "note")
    async with get_sessionmaker()() as session:
        await h.register_composite(
            session,
            d.STEP_NOTE_PARENT_OP_ID,
            "step_with_note_composite",
            module="tests._approval_secret_dispatch_helpers",
        )
        yield session
    for op_id in (d.TIMEOUT_OP_ID, d.DENY_OP_ID):
        op_classes._SECRET_FIELDS.pop(op_id, None)


async def _run_approved(op_id: str, params: dict[str, Any]) -> tuple[UUID, Any]:
    waiting = await _call(op_id, params)
    assert waiting.status == "awaiting_approval", waiting.error
    request = await h.approve(UUID(waiting.extras["approval_request_id"]))
    result = await resume_dispatch_after_approval(
        operator=h.operator(sub="op-approver"), request=request, params=None
    )
    return request.id, result


async def test_a_note_that_is_a_meho_word_keeps_an_uncertain_write_uncertain(
    ops: AsyncSession,
) -> None:
    params = {"name": "web", "note": "uncertain"}

    request_id, result = await _run_approved(d.TIMEOUT_OP_ID, params)

    assert result.status == "error"
    assert result.extras["execution_outcome"] == "uncertain"
    row = await h.request(request_id)
    assert row.resume_result is not None
    assert row.resume_result["extras"]["execution_outcome"] == "uncertain"
    with pytest.raises(RerunNotAllowedError):
        await rerun_failed_request(operator=h.operator(), request_id=request_id, params=params)
    assert d.received == [(d.TIMEOUT_OP_ID, params)]


async def test_a_long_note_in_an_http_422_error_leaves_no_part_of_it(ops: AsyncSession) -> None:
    first_line = "line one made-up-config-8812"
    note = first_line + "\n" + "y" * 3000

    _, result = await _run_approved(d.REJECT_OP_ID, {"name": "web", "note": note})

    assert result.status == "error"
    assert result.extras["http_status"] == 422
    assert first_line not in result.model_dump_json()
    stored = await _everything_stored()
    assert "Note refused" in stored
    assert first_line not in stored


async def test_an_snmp_community_pair_is_hidden_when_it_comes_back(ops: AsyncSession) -> None:
    community = "made-up-community-5521"
    params = {"name": "web", "settings": [{"name": "community", "value": community}]}

    _, result = await _run_approved(d.SETTINGS_ECHO_OP_ID, params)

    assert result.status == "ok", result.error
    assert d.received == [(d.SETTINGS_ECHO_OP_ID, params)]
    assert result.result["applied"] == [_MARKER]
    assert community not in await _everything_stored()


async def test_a_deny_that_quotes_the_note_is_hidden(ops: AsyncSession) -> None:
    secret = "made-up-deny-note-3307"

    request_id, result = await _run_approved(d.DENY_OP_ID, {"name": "web", "note": secret})

    assert result.status == "denied"
    assert result.extras["reason"] == f"the note {_MARKER} is not allowed"
    assert secret not in result.model_dump_json()
    row = await h.request(request_id)
    assert row.resume_result is not None
    assert row.resume_result["extras"]["reason"] == f"the note {_MARKER} is not allowed"
    assert secret not in await _everything_stored()


async def test_license_keys_sent_back_as_object_keys_are_hidden_and_stay_apart(
    ops: AsyncSession,
) -> None:
    keys = ["KEY-CCCC-3333-made-up", "KEY-DDDD-4444-made-up"]

    _, result = await _run_approved(d.SETTINGS_ECHO_OP_ID, {"name": "web", "licenseKeys": keys})

    assert result.status == "ok", result.error
    licenses = result.result["licenses"]
    assert set(licenses) == {_MARKER, f"{_MARKER} (2)"}
    assert all(entry == {"status": "active"} for entry in licenses.values())
    stored = await _everything_stored()
    assert "active" in stored
    assert not any(key in stored for key in keys)


async def test_a_changed_step_row_does_not_run_for_the_agent(ops: AsyncSession) -> None:
    agent = h.operator(sub="agent-requester", principal_kind=PrincipalKind.AGENT)
    await give_agent_a_known_person(h.TENANT_ID, "agent-requester")
    waiting = await _call(d.STEP_NOTE_PARENT_OP_ID, {"vm": "vm-1"}, agent)
    assert waiting.status == "awaiting_approval", waiting.error
    row = await h.request(UUID(waiting.extras["approval_request_id"]))
    assert row.execution_handle is None
    assert row.params == {"name": "vm-1", "note": d.COMPUTED_NOTE}
    await h.approve(row.id)
    async with get_sessionmaker()() as s:
        await s.execute(
            update(ApprovalRequest)
            .where(ApprovalRequest.id == row.id)
            .values(params={"name": "vm-2", "note": d.COMPUTED_NOTE})
        )
        await s.commit()

    resumed = await resume_or_surface_awaiting_approval(
        operator=agent,
        call_arguments={
            "connector_id": h.CONNECTOR_ID,
            "op_id": d.STEP_NOTE_PARENT_OP_ID,
            "params": {"vm": "vm-1"},
            "target": None,
        },
        awaiting_envelope=waiting.model_dump(mode="json"),
        timeout_seconds=2.0,
    )

    assert resumed["status"] == "denied", resumed
    assert resumed["extras"]["error_code"] == "approval_resume_params_unavailable"
    assert d.received == [(d.STEP_NOTE_PARENT_OP_ID, {"vm": "vm-1"})]
    stored = await h.request(row.id)
    assert stored.resume_result is not None
    assert stored.resume_result["extras"]["error_code"] == "approval_resume_params_unavailable"
    assert await _handoff_rows() == []


async def test_the_matcher_keeps_no_copy_of_a_secret_in_the_re_cache() -> None:
    secret = "madeupcachenote6620"

    hidden = hide_call_secret_values(
        {"text": f"saved {secret}", "n": 4711}, CallSecretValues(frozenset({secret}))
    )

    assert hidden == {"text": f"saved {_MARKER}", "n": 4711}
    caches = [value for name, value in vars(re).items() if name.startswith("_cache")]
    assert not any(secret in json.dumps([str(key) for key in cache]) for cache in caches)
