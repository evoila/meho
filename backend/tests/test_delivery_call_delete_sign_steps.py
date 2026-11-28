# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step that deletes keeps its own approval in an approved run, with the switch on.

The whole path: a service login starts the multi-step operation
``probe.composite.remote_steps`` (connector ``probehub-1.x``). Its first
step waits for a person, who approves it with a token, and the run goes on.
The step under test is ``argocd.app.sync`` (level ``dangerous``, approval
flag on), checked under connector ``probe-1.x``: through the step gate, and
as a child call (``probe.composite.remote_child``).

With ``CUSTOM_POLICY`` on, a sync with ``prune`` ``true`` has a delete sign
of its own, so it waits for its own approval, and its approval card has the
delete line. A sync without ``prune`` goes ahead under the run's approval.
With the switch off, both forms wait for their own approval, as on main,
because the step's approval flag is on.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.connectors.argocd.op_classes import SYNC_PRUNE_LINE
from meho_backplane.db.engine import get_sessionmaker
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_remote_steps_helpers as r
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step

pytestmark = pytest.mark.asyncio

_SYNC = Step("argocd.app.sync", "dangerous", True)
_PRUNE: dict[str, Any] = {"name": "web", "prune": True}
_PLAIN: dict[str, Any] = {"name": "web"}
_TYPED = {"typed": True}
_CHILD = pytest.mark.parametrize("child", [False, True], ids=["step-gate", "child-call"])


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


@_CHILD
async def test_on_a_step_that_prunes_waits_with_the_delete_line(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(
        session, stub_embedding_service, _SYNC, _TYPED, None, child=child, params=_PRUNE
    )
    assert result.status == "awaiting_approval", result.error
    own = await c.assert_waits(_SYNC, child=child, run_op_id=_run_op(child))
    assert (own.proposed_effect or {}).get("delete_wait") == SYNC_PRUNE_LINE


@_CHILD
async def test_on_a_step_without_prune_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(
        session, stub_embedding_service, _SYNC, _TYPED, None, child=child, params=_PLAIN
    )
    assert result.status == "ok", result.error
    await c.assert_went_ahead(_SYNC, child=child, run_op_id=_run_op(child))


@_CHILD
@pytest.mark.parametrize("params", [_PRUNE, _PLAIN], ids=["prune", "no-prune"])
async def test_off_both_forms_wait_as_on_main(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
    params: dict[str, Any],
) -> None:
    """As on main: the step's approval flag makes it wait, and no card has a delete line."""
    c.switch(monkeypatch, on=False)
    result = await r.run_remote(
        session, stub_embedding_service, _SYNC, _TYPED, None, child=child, params=params
    )
    assert result.status == "awaiting_approval", result.error
    own = await c.assert_waits(_SYNC, child=child, run_op_id=_run_op(child))
    assert "delete_wait" not in (own.proposed_effect or {})
