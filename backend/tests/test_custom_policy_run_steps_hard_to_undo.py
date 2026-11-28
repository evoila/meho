# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step whose operation is hard to undo keeps its own approval, with the switch on.

The whole path: a service login starts the multi-step operation
``probe.composite.remote_steps`` (connector ``probehub-1.x``). Its first
step waits for a person, who approves it with a token, and the run goes on.

* The step-gate case: the step ``POST:/probe/things/{id}/purge_old`` is
  checked under connector ``probe-1.x``. It declares level ``dangerous`` and
  no approval flag. Its stored row has ``hard_to_undo`` ``deletes`` and no
  other mark.
* The child-call case: ``probe.composite.remote_child`` calls the typed
  operation ``probe.things.wipe_old`` (level ``dangerous``, ``hard_to_undo``
  ``deletes``, no other mark).

With ``CUSTOM_POLICY`` on, ``hard_to_undo`` is a delete sign (the fixed
rule ``floor:delete_waits``), so each waits for its own approval: its
handler is not called and its own ``approval.request`` audit row exists.
Without ``hard_to_undo``, each goes ahead after an Admin's approval. With the switch off, each goes
ahead under the run's approval, as on main.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_remote_steps_helpers as r
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step

pytestmark = pytest.mark.asyncio

_PURGE_OLD = Step("POST:/probe/things/{id}/purge_old", "dangerous", False)
_WIPE_OLD = Step("probe.things.wipe_old", "dangerous", False)
_HARD_TO_UNDO = {"hard_to_undo": "deletes", "typed": True}
_NO_MARK = {"typed": True}
_CASES = pytest.mark.parametrize(
    ("step", "child"),
    [
        pytest.param(_PURGE_OLD, False, id="step-purge_old"),
        pytest.param(_WIPE_OLD, True, id="child-call-wipe_old"),
    ],
)


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from c.reset(monkeypatch)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = h.EMBEDDING
    service.encode.return_value = [h.EMBEDDING]
    service.dimension = 384
    return service


def _run_op(child: bool) -> str:
    return r.REMOTE_CHILD_RUN if child else r.REMOTE_RUN


@_CASES
async def test_on_a_step_that_is_hard_to_undo_waits_for_its_own_approval(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(
        session, stub_embedding_service, step, _HARD_TO_UNDO, None, child=child
    )
    assert result.status == "awaiting_approval", result.error
    await c.assert_waits(step, child=child, run_op_id=_run_op(child))


@_CASES
async def test_on_without_hard_to_undo_the_step_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    child: bool,
) -> None:
    """An Admin approves the run: an Admin may approve the step's ``dangerous`` level."""
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(
        session,
        stub_embedding_service,
        step,
        _NO_MARK,
        None,
        child=child,
        approver_role=TenantRole.TENANT_ADMIN,
    )
    assert result.status == "ok", result.error
    await c.assert_went_ahead(step, child=child, run_op_id=_run_op(child))


@_CASES
async def test_off_a_step_that_is_hard_to_undo_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    child: bool,
) -> None:
    """As on main: ``hard_to_undo`` changes no answer with the switch off."""
    c.switch(monkeypatch, on=False)
    result = await r.run_remote(
        session, stub_embedding_service, step, _HARD_TO_UNDO, None, child=child
    )
    assert result.status == "ok", result.error
    await c.assert_went_ahead(step, child=child, run_op_id=_run_op(child))
