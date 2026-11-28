# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The steps of an approved run, approved through the real approve routes.

A service login starts a multi-step operation. Its first step waits for a
person, who approves it with a token (``POST /api/v1/approvals/{id}/approve``)
or in the web console (``POST /ui/approvals/{id}/approve``). The route runs
the rest of the run, which reaches the step under test as a step of the
step gate or as a child call.

With ``CUSTOM_POLICY`` on:

* a change of rights or of the policy waits for its own approval, whoever
  approved the run and wherever (token or web console);
* in the run after the approval, a step that the step rule covers waits
  for its own approval also when its policy answer lets it run at once:
  a root-shell step and a delete name, both ``safe``. A plain ``safe``
  step runs at once.

With the switch off, each test asserts the fixed answer of
:data:`_OFF_WAITS_ON_MAIN`, recorded from a run on main. Each waiting step's
own ``approval.request`` audit row names the step and its request, and the
stored reason of a wait the step rule makes names the rule. All names and
values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from httpx import ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.jwt import clear_jwks_cache
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.main import app
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks

pytestmark = pytest.mark.asyncio

_DELETE_OLD = Step("probe.things.old.delete", "safe", False)
_VIEW = Step("probe.things.view", "safe", False)
_BOTH_PATHS = pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
_BOTH_STATES = pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
_RIGHTS_STEPS = pytest.mark.parametrize(
    "step", [c.RIGHTS, c.POLICY], ids=["change-of-rights", "change-of-the-policy"]
)


@pytest.fixture(autouse=True)
def _state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    yield from c.reset(monkeypatch)
    clear_jwks_cache()


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


@pytest.fixture
async def client() -> AsyncIterator[tuple[httpx.AsyncClient, Any]]:
    clear_jwks_cache()
    key = make_rsa_keypair("kid-probe-run-steps")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _approved_with_token(
    client: tuple[httpx.AsyncClient, Any],
    session: AsyncSession,
    stub: AsyncMock,
    step: Step,
    *,
    child: bool,
) -> str:
    """Start the run, approve its first step on the token route; give back the run's status."""
    await c.register(session, stub, step, child=child)
    request_id = await c.start_run(c.run_op(child=child))
    response = await c.approve_with_token(client[0], client[1], request_id)
    status: str = response.json()["dispatch_status"]
    return status


#: With the switch off: does the step wait for its own approval? One entry per
#: approve route, step and path. Recorded from a run of these tests on main,
#: where the step rule did not exist; no entry comes from the rule under test.
_OFF_WAITS_ON_MAIN: dict[tuple[str, str, str], bool] = {
    ("console", c.RIGHTS.op_id, "step"): False,
    ("console", c.RIGHTS.op_id, "child-call"): True,
    ("console", c.POLICY.op_id, "step"): False,
    ("console", c.POLICY.op_id, "child-call"): True,
    ("token", c.RIGHTS.op_id, "step"): False,
    ("token", c.RIGHTS.op_id, "child-call"): True,
    ("token", c.POLICY.op_id, "step"): False,
    ("token", c.POLICY.op_id, "child-call"): True,
    ("token", c.SNAPSHOT_SAVE.op_id, "step"): False,
    ("token", c.SNAPSHOT_SAVE.op_id, "child-call"): False,
    ("token", _DELETE_OLD.op_id, "step"): False,
    ("token", _DELETE_OLD.op_id, "child-call"): False,
    ("token", _VIEW.op_id, "step"): False,
    ("token", _VIEW.op_id, "child-call"): False,
}


def _expected_wait(route: str, step: Step, *, child: bool, on: bool, waits_on: bool) -> bool:
    """Switch on: *waits_on*. Switch off: the fixed answer of :data:`_OFF_WAITS_ON_MAIN`."""
    if on:
        return waits_on
    return _OFF_WAITS_ON_MAIN[(route, step.op_id, "child-call" if child else "step")]


def _rule_reason(on: bool, rule: str) -> str | None:
    """The stored reason of a wait the step rule makes; ``None`` with the switch off."""
    return c.STEP_RULE_WAIT.format(rule) if on else None


async def _assert(step: Step, *, child: bool, waits: bool, wait_reason: str | None = None) -> None:
    if waits:
        await c.assert_waits(step, child=child, wait_reason=wait_reason)
    else:
        await c.assert_went_ahead(step, child=child)


# -- point 1: a change of rights or of the policy -----------------------------------


@_RIGHTS_STEPS
@_BOTH_STATES
@_BOTH_PATHS
async def test_a_change_of_rights_after_an_approval_in_the_web_console(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    c.console_env(monkeypatch)
    await c.register(session, stub_embedding_service, step, child=child)
    request_id = await c.start_run(c.run_op(child=child))

    await c.approve_in_console(request_id, c.console_person())

    waits = _expected_wait("console", step, child=child, on=on, waits_on=True)
    await _assert(step, child=child, waits=waits)


@_RIGHTS_STEPS
@_BOTH_STATES
@_BOTH_PATHS
async def test_a_change_of_rights_after_an_approval_with_a_token(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[httpx.AsyncClient, Any],
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    status = await _approved_with_token(client, session, stub_embedding_service, step, child=child)
    waits = _expected_wait("token", step, child=child, on=on, waits_on=True)
    assert status == ("awaiting_approval" if waits else "ok")
    await _assert(step, child=child, waits=waits)


# -- point 5: a step whose first answer lets it run at once --------------------------


@pytest.mark.parametrize(
    ("step", "waits_on", "rule"),
    [
        (c.SNAPSHOT_SAVE, True, "no_run_approval_covers_it"),
        (_DELETE_OLD, True, "floor:delete_waits"),
        (_VIEW, False, None),
    ],
    ids=["root-shell", "delete-name", "plain"],
)
@_BOTH_STATES
@_BOTH_PATHS
async def test_a_safe_step_after_an_approval_with_a_token(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[httpx.AsyncClient, Any],
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    waits_on: bool,
    rule: str | None,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    waits = _expected_wait("token", step, child=child, on=on, waits_on=waits_on)
    status = await _approved_with_token(client, session, stub_embedding_service, step, child=child)
    assert status == ("awaiting_approval" if waits else "ok")
    # The step's own ``approval.request`` row names the step rule as the reason of the wait.
    reason = _rule_reason(on, rule) if rule is not None else None
    await _assert(step, child=child, waits=waits, wait_reason=reason)
