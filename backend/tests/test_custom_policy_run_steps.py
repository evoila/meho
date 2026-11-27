# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, each step of an approved run asks the fixed rules.

The whole path: a service login calls a multi-step operation. Its first
step waits for a person. A person approves it, and the run goes on under
that approval (``resume_dispatch_after_approval``). Then the run reaches
the step under test, as a step of the step gate
(:func:`~meho_backplane.operations.composite.enforce_subop_policy`) or as a
child call (``dispatch_child``). The tests read the stored approval
requests and the audit rows. The cases that depend on how the approver
signed in go through the approve routes:
``test_custom_policy_run_steps_routes.py`` and
``test_custom_policy_run_steps_console_rights.py``.

With the switch on:

* a step whose operation has only the plain "always ask" mark goes ahead
  under the run's approval;
* a step that a fixed rule covers (a delete name, a change of rights, a
  credential call, a call to an address the caller gives, a stored
  ``destructive`` tag or ``DELETE`` verb) waits for its own approval, also
  when only the stored row of a step-gate step says so;
* a root-shell step and a step an override made wait still wait, also an
  override row of the caller's tenant;
* for a step-gate step, the facts of the built-in row and of the caller's
  tenant's own row both count (cases R1 to R4).

Each test that asserts a wait also reads the step's own ``approval.request``
audit row (``own.request_audit_id``): it names the step and its request.

With the switch off, each test asserts the answer of the code on main,
and the step rule reads no stored row.

All names and values are made up.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import PrincipalKind
from meho_backplane.connectors.registry import register_connector_v2
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor, OperationPolicyOverride
from meho_backplane.operations import dispatch, run_step_rules
from meho_backplane.operations.approval_queue import resume_dispatch_after_approval
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.operations.composite import composite_resume_state, enforce_subop_policy
from tests import _composite_wait_audit_helpers as h
from tests import _custom_policy_run_steps_helpers as c
from tests._custom_policy_run_steps_helpers import Step
from tests._people_behind_helpers import give_service_a_known_person

pytestmark = pytest.mark.asyncio

_PLAIN = Step("probe.things.label", "safe", True)
_PLAIN_CAUTION = Step("probe.things.label", "caution", True)
_DELETE_NAME = Step("probe.things.delete", "dangerous", False)
_CREDENTIAL = Step("vault.token.create", "safe", False)
_OVERRIDDEN = Step("probe.things.resize", "safe", False)
_CALLER_ADDRESS = Step("probe.things.fetch", "dangerous", False)
_TAGGED = Step("probe.things.zap", "dangerous", False)
_DELETE_VERB = Step("probe.things.drop", "dangerous", False)
_OTHER_TENANT = UUID("00000000-0000-0000-0000-0000000000f6")
_ADDRESS_SCHEMA = {
    "type": "object",
    "properties": {"url": {"type": "string", CALLER_ADDRESS_MARKER: True}},
}
_BOTH_PATHS = pytest.mark.parametrize("child", [False, True], ids=["step", "child-call"])
_BOTH_STATES = pytest.mark.parametrize("on", [True, False], ids=["switch-on", "switch-off"])


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


async def _run_step(
    session: AsyncSession, stub: AsyncMock, step: Step, *, child: bool = False, **stored: Any
) -> Any:
    """Register *step*, start the run, approve its first step, and let the run go on."""
    await c.register(session, stub, step, child=child, **stored)
    request_id = await c.start_run(c.run_op(child=child))
    request = await h.approve(request_id, sub=c.APPROVER_SUB)
    return await resume_dispatch_after_approval(
        operator=c.token_approver(), request=request, params=None
    )


async def _went_ahead(result: Any, step: Step, *, child: bool) -> None:
    assert result.status == "ok", result.error
    await c.assert_went_ahead(step, child=child)


async def _waits(result: Any, step: Step, *, child: bool) -> None:
    assert result.status == "awaiting_approval", result.error
    own = await c.assert_waits(step, child=child)
    assert str(own.id) == result.extras["approval_request_id"]
    h.assert_wait_row((await h.rows(c.run_op(child=child)))[-1], result)


@_BOTH_PATHS
async def test_on_a_step_with_only_the_plain_mark_goes_ahead_under_the_run_approval(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await _run_step(session, stub_embedding_service, _PLAIN, child=child)
    await _went_ahead(result, _PLAIN, child=child)


@_BOTH_PATHS
async def test_off_a_step_with_the_plain_mark_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=False)
    result = await _run_step(session, stub_embedding_service, _PLAIN, child=child)
    await _waits(result, _PLAIN, child=child)


@pytest.mark.parametrize(
    "step", [_DELETE_NAME, c.RIGHTS, c.POLICY, _CREDENTIAL, c.ROOT_SHELL], ids=lambda s: s.op_id
)
@_BOTH_PATHS
async def test_on_a_step_a_fixed_rule_or_the_root_shell_mark_covers_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await _run_step(session, stub_embedding_service, step, child=child)
    await _waits(result, step, child=child)


@pytest.mark.parametrize(
    ("step", "child"),
    [
        (_DELETE_NAME, False),
        (c.RIGHTS, False),
        (c.ROOT_SHELL, False),
        (_DELETE_NAME, True),
        (c.ROOT_SHELL, True),
    ],
    ids=["step-delete", "step-rights", "step-root-shell", "child-delete", "child-root-shell"],
)
async def test_off_the_same_steps_go_ahead_under_the_run_approval(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=False)
    result = await _run_step(session, stub_embedding_service, step, child=child)
    await _went_ahead(result, step, child=child)


@_BOTH_STATES
async def test_a_credential_step_waits_in_both_switch_states(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    result = await _run_step(session, stub_embedding_service, _CREDENTIAL)
    await _waits(result, _CREDENTIAL, child=False)


# -- the stored facts of the step's operation ------------------------------------

#: The stored rows of cases 3 and 4: the step, and what its stored row holds.
_STORED_CASES = [
    pytest.param(_CALLER_ADDRESS, {"schema": _ADDRESS_SCHEMA}, id="caller-address"),
    pytest.param(_TAGGED, {"tags": ["destructive"]}, id="destructive-tag"),
    pytest.param(_DELETE_VERB, {"method": "DELETE"}, id="delete-verb"),
]


@pytest.mark.parametrize(("step", "stored"), _STORED_CASES)
@_BOTH_PATHS
async def test_on_a_step_whose_stored_row_a_fixed_rule_covers_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    stored: dict[str, Any],
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    result = await _run_step(
        session, stub_embedding_service, step, child=child, stored=True, **stored
    )
    await _waits(result, step, child=child)


class _Reads:
    """Counts the reads of a step's stored rows by the step rule, and the rows it gets."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads: list[str] = []
        self.rows: list[tuple[EndpointDescriptor, list[EndpointDescriptor]]] = []
        real_read = run_step_rules._stored_step_rows
        real_rows = run_step_rules.step_rule_rows

        async def _read(step: EndpointDescriptor, tenant_id: Any) -> Any:
            if step.op_id != h.STEP_A:
                self.reads.append(step.op_id)
            return await real_read(step, tenant_id)

        async def _rows(step: EndpointDescriptor, tenant_id: Any) -> list[EndpointDescriptor]:
            copies = await real_rows(step, tenant_id)
            if step.op_id != h.STEP_A:
                self.rows.append((step, copies))
            return copies

        monkeypatch.setattr(run_step_rules, "_stored_step_rows", _read)
        monkeypatch.setattr(run_step_rules, "step_rule_rows", _rows)


@pytest.mark.parametrize(("step", "stored"), _STORED_CASES)
async def test_off_the_step_rule_reads_no_stored_row_and_the_step_goes_ahead(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    stored: dict[str, Any],
) -> None:
    c.switch(monkeypatch, on=False)
    reads = _Reads(monkeypatch)
    result = await _run_step(session, stub_embedding_service, step, stored=True, **stored)
    assert reads.reads == []
    [(row, copies)] = reads.rows
    assert copies == [row]
    assert copies[0] is row
    await _went_ahead(result, step, child=False)


@pytest.mark.parametrize(("step", "stored"), _STORED_CASES)
async def test_on_the_stored_facts_go_only_into_the_copy_the_step_rule_reads(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    stored: dict[str, Any],
) -> None:
    c.switch(monkeypatch, on=True)
    reads = _Reads(monkeypatch)
    result = await _run_step(session, stub_embedding_service, step, stored=True, **stored)
    assert reads.reads == [step.op_id]
    [(row, [copy])] = reads.rows
    assert copy is not row
    assert (row.parameter_schema, row.method, row.tags) == ({}, None, None)
    assert copy.parameter_schema == stored.get("schema", {"type": "object"})
    assert copy.method == stored.get("method")
    assert copy.tags == stored.get("tags", [])
    _, own = await c.requests()
    assert own.proposed_effect["safety_level"] == step.safety_level
    await _waits(result, step, child=False)


# -- override rows -----------------------------------------------------------------


async def _override(step: Step, *, tenant_id: UUID | None = None, **fields: Any) -> None:
    async with get_sessionmaker()() as s, s.begin():
        s.add(
            OperationPolicyOverride(
                tenant_id=tenant_id,
                product="vault",
                version="1.x",
                impl_id="vault",
                op_id=step.op_id,
                set_by_sub="admin-a",
                **fields,
            )
        )


@_BOTH_STATES
@_BOTH_PATHS
async def test_a_step_an_override_made_wait_still_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    await _override(_OVERRIDDEN, requires_approval=True)
    result = await _run_step(session, stub_embedding_service, _OVERRIDDEN, child=child)
    await _waits(result, _OVERRIDDEN, child=child)


@_BOTH_PATHS
async def test_on_a_plain_mark_step_with_an_override_flag_too_still_waits(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    await _override(_PLAIN, requires_approval=True)
    result = await _run_step(session, stub_embedding_service, _PLAIN, child=child)
    await _waits(result, _PLAIN, child=child)


@pytest.mark.parametrize(
    ("tenant_id", "waits"),
    [(h.TENANT_ID, True), (_OTHER_TENANT, False)],
    ids=["override-of-the-caller-s-tenant", "override-of-another-tenant"],
)
@_BOTH_PATHS
async def test_on_a_plain_mark_step_with_a_tenant_override_flag(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    tenant_id: UUID,
    waits: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=True)
    await _override(_PLAIN_CAUTION, tenant_id=tenant_id, requires_approval=True)
    result = await _run_step(session, stub_embedding_service, _PLAIN_CAUTION, child=child)
    if waits:
        await _waits(result, _PLAIN_CAUTION, child=child)
    else:
        await _went_ahead(result, _PLAIN_CAUTION, child=child)


@_BOTH_STATES
@_BOTH_PATHS
async def test_a_step_an_override_raised_to_destructive_is_denied(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    on: bool,
    child: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    await _override(_OVERRIDDEN, safety_level="destructive")
    result = await _run_step(session, stub_embedding_service, _OVERRIDDEN, child=child)
    assert result.status == "denied", result
    assert c.ran == [h.STEP_A]
    assert len(await c.requests()) == 1
    step_rows = await h.rows(_OVERRIDDEN.op_id)
    assert [r.payload["result_status"] for r in step_rows] == ["denied"]


# -- a built-in row and the tenant's own row of a step-gate step ------------------

_REMOTE_RUN = "probe.composite.remote_steps"
_REMOTE_CONNECTOR = "probehub-1.x"
_FETCH = Step("POST:/probe/things/{id}/fetch", "dangerous", False)
_ZAP = Step("POST:/probe/things/{id}/zap", "dangerous", False)
_PLAIN_SCHEMA: dict[str, Any] = {"type": "object", "properties": {"url": {"type": "string"}}}


class _ProbeHubConnector(h.NoOpVaultConnector):
    product = "probehub"
    version = "1.x"
    impl_id = "probehub"


async def remote_steps(operator: Any, target: Any, params: dict[str, Any]) -> Any:
    """A step that waits, then the step under test, checked under connector ``probe-1.x``."""
    if composite_resume_state(_REMOTE_RUN) is None:
        gated = await enforce_subop_policy(
            operator=operator,
            connector_id=_REMOTE_CONNECTOR,
            op_id=h.STEP_A,
            safety_level="safe",
            requires_approval=True,
            target=target,
            params={"name": "vm-1"},
        )
        if gated is not None:
            return gated
        c.ran.append(h.STEP_A)
    [step] = c.current
    gated = await enforce_subop_policy(
        operator=operator,
        connector_id="probe-1.x",
        op_id=step.op_id,
        safety_level=step.safety_level,
        requires_approval=step.requires_approval,
        target=target,
        params={},
        resume_state={"at": "remote"},
        after_main_effect=True,
    )
    if gated is not None:
        return gated
    c.ran.append(step.op_id)
    return {"done": True}


def _probe_row(step: Step, tenant_id: UUID | None, facts: dict[str, Any]) -> EndpointDescriptor:
    """A stored row of the step's operation under ``probe-1.x``."""
    method, path = step.op_id.split(":", 1)
    return EndpointDescriptor(
        tenant_id=tenant_id,
        product="probe",
        version="1.x",
        impl_id="probe",
        op_id=step.op_id,
        source_kind="ingested",
        method=method,
        path=path,
        summary="The step under test.",
        description="The step under test.",
        tags=facts.get("tags", []),
        parameter_schema=facts.get("schema", _PLAIN_SCHEMA),
        safety_level=step.safety_level,
        requires_approval=step.requires_approval,
        is_enabled=True,
        embedding=h.EMBEDDING,
    )


async def _run_remote(
    session: AsyncSession,
    stub: AsyncMock,
    step: Step,
    built_in: dict[str, Any] | None,
    tenant_row: dict[str, Any] | None,
) -> Any:
    """Store the rows, start the run as a service login, approve with a token, go on."""
    await h.register_child(stub)
    register_connector_v2(
        product="probehub", version="1.x", impl_id="probehub", cls=_ProbeHubConnector
    )
    async with get_sessionmaker()() as s, s.begin():
        if built_in is not None:
            s.add(_probe_row(step, None, built_in))
        if tenant_row is not None:
            s.add(_probe_row(step, h.TENANT_ID, tenant_row))
    session.add(
        EndpointDescriptor(
            tenant_id=None,
            product="probehub",
            version="1.x",
            impl_id="probehub",
            op_id=_REMOTE_RUN,
            source_kind="composite",
            handler_ref=f"{__name__}.remote_steps",
            summary="The run.",
            description="The run.",
            tags=[],
            parameter_schema={"type": "object"},
            safety_level="safe",
            requires_approval=False,
            is_enabled=True,
            embedding=h.EMBEDDING,
        )
    )
    await session.commit()
    c.current.append(step)
    service = h.operator(sub=c.SERVICE_SUB, principal_kind=PrincipalKind.SERVICE)
    await give_service_a_known_person(h.TENANT_ID, c.SERVICE_SUB)
    first = await dispatch(
        operator=service, connector_id=_REMOTE_CONNECTOR, op_id=_REMOTE_RUN, target=None, params={}
    )
    assert first.status == "awaiting_approval", first.error
    request = await h.approve(UUID(first.extras["approval_request_id"]), sub=c.APPROVER_SUB)
    return await resume_dispatch_after_approval(
        operator=c.token_approver(), request=request, params=None
    )


_ADDRESS = {"schema": _ADDRESS_SCHEMA}
_DESTRUCTIVE = {"tags": ["destructive"]}
_NO_MARK: dict[str, Any] = {}
#: Cases R1 to R4: the step, the facts of the built-in row and of the tenant's
#: own row, and whether the step waits with the switch on.
_TWO_ROW_CASES = [
    pytest.param(_FETCH, _ADDRESS, _NO_MARK, True, id="R1-address-on-the-built-in-row"),
    pytest.param(_FETCH, _NO_MARK, _ADDRESS, True, id="R2-address-on-the-tenant-row"),
    pytest.param(_ZAP, _DESTRUCTIVE, _NO_MARK, True, id="R3-destructive-on-the-built-in-row"),
    pytest.param(_ZAP, _NO_MARK, _DESTRUCTIVE, True, id="R3-destructive-on-the-tenant-row"),
    pytest.param(_FETCH, _NO_MARK, _NO_MARK, False, id="R4-no-mark"),
]


@pytest.mark.parametrize(("step", "built_in", "tenant_row", "waits_on"), _TWO_ROW_CASES)
@_BOTH_STATES
async def test_the_facts_of_the_built_in_row_and_the_tenant_row_both_count(
    monkeypatch: pytest.MonkeyPatch,
    stub_embedding_service: AsyncMock,
    session: AsyncSession,
    step: Step,
    built_in: dict[str, Any],
    tenant_row: dict[str, Any],
    waits_on: bool,
    on: bool,
) -> None:
    c.switch(monkeypatch, on=on)
    reads = _Reads(monkeypatch)
    result = await _run_remote(session, stub_embedding_service, step, built_in, tenant_row)
    if on:
        assert reads.reads == [step.op_id]
        [(_, copies)] = reads.rows
        assert len(copies) == 2
    else:
        assert reads.reads == []
    if on and waits_on:
        assert result.status == "awaiting_approval", result.error
        await c.assert_waits(step, child=False, run_op_id=_REMOTE_RUN)
    else:
        assert result.status == "ok", result.error
        await c.assert_went_ahead(step, child=False, run_op_id=_REMOTE_RUN)
