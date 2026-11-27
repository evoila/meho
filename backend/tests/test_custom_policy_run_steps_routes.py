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

With the switch off, each test asserts the answer of the code with the
switch off. All names and values are made up.
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


def _expected_wait(on: bool, answers: list[bool]) -> bool:
    """Switch on: the step waits. Switch off: the answer of the rule with the switch off.

    With the switch off, ``step_keeps_own_approval`` gives the row's
    ``requires_approval``: the declared flag of a step-gate step, the
    stored row's flag of a child call.
    """
    if on:
        return True
    assert answers, "the rule was not asked"
    return answers[-1]


async def _assert(step: Step, *, child: bool, waits: bool) -> None:
    if waits:
        await c.assert_waits(step, child=child)
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
    answers = c.record_rule_answers(monkeypatch)
    await c.register(session, stub_embedding_service, step, child=child)
    request_id = await c.start_run(c.run_op(child=child))

    await c.approve_in_console(request_id, c.console_person())

    await _assert(step, child=child, waits=_expected_wait(on, answers))


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
    answers = c.record_rule_answers(monkeypatch)
    status = await _approved_with_token(client, session, stub_embedding_service, step, child=child)
    waits = _expected_wait(on, answers)
    assert status == ("awaiting_approval" if waits else "ok")
    await _assert(step, child=child, waits=waits)


# -- point 5: a step whose first answer lets it run at once --------------------------


@pytest.mark.parametrize(
    ("step", "waits_on"),
    [(c.SNAPSHOT_SAVE, True), (_DELETE_OLD, True), (_VIEW, False)],
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
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    waits = on and waits_on
    status = await _approved_with_token(client, session, stub_embedding_service, step, child=child)
    assert status == ("awaiting_approval" if waits else "ok")
    await _assert(step, child=child, waits=waits)
