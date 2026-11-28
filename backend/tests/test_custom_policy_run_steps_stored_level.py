# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The stored risk level of a step's operation counts for the step rule.

The whole path: a service login starts the multi-step operation
``probe.composite.remote_steps`` (connector ``probehub-1.x``). Its first
step waits for a person, who approves it with a token, and the run goes on
(``resume_dispatch_after_approval``). Its second step,
``POST:/probe/things/{id}/wipe``, is checked under connector ``probe-1.x``.
It declares level ``safe`` and no approval flag; no stored row has another
mark. The same operation also runs as a child call of
``probe.composite.remote_child`` in the same kind of run.

With ``CUSTOM_POLICY`` on, the copy of a stored row that the step rule reads
has the higher of the declared and the stored level:

* L1, a stored ``destructive`` level on the built-in row, and L2, on the
  caller's tenant's own row: the step does not go ahead under the run's
  approval. A ``destructive`` call cannot wait without a preview hash, so
  the step gets the refusal ``preview_binding_required``, the same answer
  and audit row as a child call of the same operation.
* L3, both rows ``safe``: the step goes ahead under the run's approval.

With the switch off, the step goes ahead in each case, as on main, and the
step rule reads no stored row.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import run_step_rules
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_remote_steps_helpers as r
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step

pytestmark = pytest.mark.asyncio

_WIPE = Step("POST:/probe/things/{id}/wipe", "safe", False)
_SAFE = {"level": "safe", "typed": True}
_DESTRUCTIVE = {"level": "destructive", "typed": True}
#: Cases L1 to L3: the stored level of the built-in row and of the tenant's own row.
_L1 = pytest.param(_DESTRUCTIVE, None, id="L1-destructive-on-the-built-in-row")
_L2 = pytest.param(_SAFE, _DESTRUCTIVE, id="L2-destructive-on-the-tenant-row")
_L3 = pytest.param(_SAFE, _SAFE, id="L3-safe-on-both-rows")
_MEMBER_ADD = Step("probe.things.member_add", "safe", False)
_BOTH_PATHS = pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])


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


def _count_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The operations whose stored rows the step rule reads, in order."""
    reads: list[str] = []
    real = run_step_rules._stored_step_rows

    async def _read(step: EndpointDescriptor, tenant_id: Any) -> Any:
        if step.op_id != h.STEP_A:
            reads.append(step.op_id)
        return await real(step, tenant_id)

    monkeypatch.setattr(run_step_rules, "_stored_step_rows", _read)
    return reads


@pytest.mark.parametrize(("built_in", "tenant_row"), [_L1, _L2])
@_BOTH_PATHS
async def test_on_a_stored_destructive_level_refuses_the_step_as_a_child_call(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    built_in: dict[str, Any],
    tenant_row: dict[str, Any] | None,
    child: bool,
) -> None:
    """The step and a child call of the same operation get one answer: the refusal."""
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(
        session, stub_embedding_service, _WIPE, built_in, tenant_row, child=child
    )
    assert result.status == "denied", result
    assert result.extras["error_code"] == "preview_binding_required"
    assert c.ran == [h.STEP_A]
    assert len(await c.requests()) == 1
    [step_row] = await h.rows(_WIPE.op_id)
    assert step_row.payload["result_status"] == "denied"
    assert step_row.payload["error"]["error_code"] == "preview_binding_required"


@_BOTH_PATHS
async def test_on_both_rows_safe_the_step_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await r.run_remote(session, stub_embedding_service, _WIPE, _SAFE, _SAFE, child=child)
    assert result.status == "ok", result.error
    run_op_id = r.REMOTE_CHILD_RUN if child else r.REMOTE_RUN
    await c.assert_went_ahead(_WIPE, child=child, run_op_id=run_op_id)


@pytest.mark.parametrize(("built_in", "tenant_row"), [_L1, _L2, _L3])
async def test_off_the_step_goes_ahead_and_no_stored_row_is_read(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    built_in: dict[str, Any],
    tenant_row: dict[str, Any] | None,
) -> None:
    c.switch(monkeypatch, on=False)
    reads = _count_reads(monkeypatch)
    result = await r.run_remote(session, stub_embedding_service, _WIPE, built_in, tenant_row)
    assert reads == []
    assert result.status == "ok", result.error
    await c.assert_went_ahead(_WIPE, child=False, run_op_id=r.REMOTE_RUN)


@pytest.mark.parametrize(("built_in", "tenant_row"), [_L1, _L2, _L3])
async def test_on_the_step_rule_reads_the_stored_level_and_the_card_keeps_the_declared_one(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    built_in: dict[str, Any],
    tenant_row: dict[str, Any] | None,
) -> None:
    """Each copy has the higher level; the step's own row keeps ``safe``."""
    c.switch(monkeypatch, on=True)
    seen: list[tuple[str, list[str]]] = []
    real = run_step_rules.step_rule_rows

    async def _rows(step: EndpointDescriptor, tenant_id: Any) -> list[EndpointDescriptor]:
        copies = await real(step, tenant_id)
        if step.op_id == _WIPE.op_id:
            seen.append((step.safety_level, [row.safety_level for row in copies]))
        return copies

    monkeypatch.setattr(run_step_rules, "step_rule_rows", _rows)
    await r.run_remote(session, stub_embedding_service, _WIPE, built_in, tenant_row)
    stored = [row["level"] for row in (built_in, tenant_row) if row is not None]
    [(declared, levels)] = seen
    assert declared == "safe"
    assert sorted(levels) == sorted(stored)


@pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])
async def test_a_step_whose_stored_row_changes_rights_waits_with_the_switch_on(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    """A ``safe`` step whose stored row has ``changes_rights`` waits by ``target_rights_waits``.

    A tenant admin approves the run, so only the rule for a change of rights
    in a target system can make the step wait.
    """
    c.switch(monkeypatch, on=on)
    rights = {"level": "safe", "typed": True, "changes_rights": True}
    result = await r.run_remote(
        session,
        stub_embedding_service,
        _MEMBER_ADD,
        rights,
        None,
        approver_role=TenantRole.TENANT_ADMIN,
    )
    if on:
        assert result.status == "awaiting_approval", result
        await c.assert_waits(
            _MEMBER_ADD,
            child=False,
            wait_reason=c.STEP_RULE_WAIT.format("floor:target_rights_waits"),
            run_op_id=r.REMOTE_RUN,
        )
    else:
        assert result.status == "ok", result.error
        await c.assert_went_ahead(_MEMBER_ADD, child=False, run_op_id=r.REMOTE_RUN)
