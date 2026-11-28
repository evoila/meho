# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A step whose level an override raised, in a run a person approved.

The whole path: a service login starts a multi-step operation. Its first
step waits for a person, who approves it with a token
(``POST /api/v1/approvals/{id}/approve``) or in the web console
(``POST /ui/approvals/{id}/approve``). The person is a tenant Admin or an
Operator. An Operator may not approve another person's ``dangerous``
change. The route runs the rest of the run, which reaches the step under
test through the step gate or as a child call.

The step declares level ``safe`` and no approval flag. An install-wide
override row raises its level to ``dangerous`` and sets no approval flag.

With ``CUSTOM_POLICY`` on, the step goes ahead under the run's approval only
when the person who approved the run may approve ``dangerous``: an Admin.
After an Operator's approval it waits for its own approval: its own
request carries the raised level, and the handler is not called.

With the switch off, the step goes ahead under the run's approval for each
approver, route and path, as on main.

All names and values are made up.
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
from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import OperationPolicyOverride
from meho_backplane.main import app
from meho_backplane.operations.approved_run_rights import run_approver_may_approve
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._oidc_jwt_helpers import make_rsa_keypair, mock_discovery_and_jwks, public_jwks

pytestmark = pytest.mark.asyncio

_GROW = Step("probe.things.grow", "safe", False)
_BOTH_PATHS = pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
_BOTH_STATES = pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
_BOTH_APPROVERS = pytest.mark.parametrize(
    "role", [TenantRole.TENANT_ADMIN, TenantRole.OPERATOR], ids=["admin", "operator"]
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
    key = make_rsa_keypair("kid-probe-raised-level")
    with respx.mock as r:
        mock_discovery_and_jwks(r, public_jwks(key))
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="https://testserver"
        ) as ac:
            yield ac, key


async def _raised_run(session: AsyncSession, stub: AsyncMock, *, child: bool) -> Any:
    """Register the run and the step, raise the step to ``dangerous``, start the run."""
    await c.register(session, stub, _GROW, child=child)
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationPolicyOverride(
                tenant_id=None,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=_GROW.op_id,
                safety_level="dangerous",
                set_by_sub="admin-a",
            )
        )
    return await c.start_run(c.run_op(child=child))


def _waits(*, on: bool, role: TenantRole) -> bool:
    """Switch on: the step waits after an Operator's approval. Switch off: never, as on main."""
    return on and role is TenantRole.OPERATOR


async def _assert(*, child: bool, waits: bool) -> None:
    if waits:
        own = await c.assert_waits(_GROW, child=child)
        assert own.proposed_effect["safety_level"] == "dangerous"
    else:
        await c.assert_went_ahead(_GROW, child=child)
    assert c.ran.count(_GROW.op_id) == (0 if waits else 1)


@_BOTH_APPROVERS
@_BOTH_STATES
@_BOTH_PATHS
async def test_a_raised_step_after_an_approval_with_a_token(
    monkeypatch: pytest.MonkeyPatch,
    client: tuple[httpx.AsyncClient, Any],
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    role: TenantRole,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    request_id = await _raised_run(session, stub_embedding_service, child=child)

    response = await c.approve_with_token(client[0], client[1], request_id, role=role)

    waits = _waits(on=on, role=role)
    assert response.json()["dispatch_status"] == ("awaiting_approval" if waits else "ok")
    await _assert(child=child, waits=waits)


@_BOTH_APPROVERS
@_BOTH_STATES
@_BOTH_PATHS
async def test_a_raised_step_after_an_approval_in_the_web_console(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    role: TenantRole,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    c.console_env(monkeypatch)
    request_id = await _raised_run(session, stub_embedding_service, child=child)

    await c.approve_in_console(request_id, c.console_person(role=role))

    await _assert(child=child, waits=_waits(on=on, role=role))


@pytest.mark.parametrize(
    ("level", "may"),
    [("safe", True), ("caution", True), ("dangerous", False), ("destructive", False)],
)
async def test_with_no_bound_approval_only_a_low_level_counts_as_approved(
    level: str, may: bool
) -> None:
    assert await run_approver_may_approve(level) is may
