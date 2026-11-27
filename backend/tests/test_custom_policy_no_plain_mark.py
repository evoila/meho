# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""With ``CUSTOM_POLICY`` on, the plain "always ask" mark counts as the level ``dangerous``.

The plain mark is ``requires_approval`` from the code with no reason behind
it (:func:`~meho_backplane.operations.op_policy.has_plain_mark`). These
tests use every operation the typed connectors of this repository register,
so the set is never a written list:

* each ``safe`` or ``caution`` operation with the plain mark has the
  effective level ``dangerous``, and the effective approval flag no longer
  holds the mark; every other operation keeps the values it has with the
  switch off;
* an imported ``caution`` operation with the plain mark becomes
  ``dangerous`` too, a ``safe`` one as well;
* the real gate (``policy_gate``) answers each kind of caller for each
  operation at least as strictly as with the switch off, and more strictly
  only for an operation with ``hard_to_undo``; the switch makes no new
  difference between the gate and the policy evaluator (``meho policy
  explain``, the shadow check);
* a step of a multi-step operation and the dry run of a policy edit
  (``policy_in_force``) use the same rule; a step with ``hard_to_undo``
  keeps its level and its mark.

The calls that go the whole way (REST, MCP, the web console) are in
``test_custom_policy_no_plain_mark_calls.py``.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import pytest
from sqlalchemy import select

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    EndpointDescriptor,
    PermissionVerdict,
    ServicePrincipalGrant,
)
from meho_backplane.operations._validate import policy_gate
from meho_backplane.operations.op_policy import (
    apply_effective_policy,
    apply_step_policy,
    has_plain_mark,
    plain_mark_counts_as_level,
    policy_in_force,
)
from meho_backplane.operations.typed_register import clear_typed_op_snapshot
from meho_backplane.policy.classification import DEFAULT_RULE_REF, Classification
from meho_backplane.policy.evaluate import Verdict
from meho_backplane.policy.shadow import evaluate_call
from meho_backplane.policy.store import connector_id_of
from meho_backplane.settings import get_settings
from tests.approval_table import register_all_descriptors

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000007062")
_AGENT = "agent:plain-mark"
_SERVICE = "svc:plain-mark"
_RANK = {
    PermissionVerdict.AUTO_EXECUTE: 0,
    PermissionVerdict.NEEDS_APPROVAL: 1,
    PermissionVerdict.DENY: 2,
}
_TO_EVALUATOR = {
    PermissionVerdict.AUTO_EXECUTE: Verdict.RUN,
    PermissionVerdict.NEEDS_APPROVAL: Verdict.WAIT,
    PermissionVerdict.DENY: Verdict.DENY,
}


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    monkeypatch.setenv("CUSTOM_POLICY", "true" if on else "false")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "off")
    # Every operation registers, the policy operations too.
    monkeypatch.setenv("POLICY_CLASSIFICATION", "true")
    _switch(monkeypatch, on=False)
    clear_typed_op_snapshot()
    yield
    clear_typed_op_snapshot()
    get_settings.cache_clear()


async def _effective_rows(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> list[Any]:
    """Every built-in operation with its effective values for the tenant, in one switch state."""
    _switch(monkeypatch, on=on)
    async with get_sessionmaker()() as session:
        rows = list(
            (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.tenant_id.is_(None))
                )
            ).scalars()
        )
        return [await apply_effective_policy(session, tenant_id=_TENANT, row=r) for r in rows]


def _key(row: Any) -> tuple[str, str]:
    return connector_id_of(row), row.op_id


@pytest.mark.asyncio
async def test_each_plain_marked_safe_or_caution_op_counts_as_dangerous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await register_all_descriptors()
    off = {_key(r): r for r in await _effective_rows(monkeypatch, on=False)}
    on = {_key(r): r for r in await _effective_rows(monkeypatch, on=True)}
    assert off.keys() == on.keys()

    relevelled = []
    for key, row in on.items():
        before = off[key]
        if has_plain_mark(row) and before.safety_level in ("safe", "caution"):
            relevelled.append(key)
            assert (row.safety_level, row.requires_approval) == ("dangerous", False), key
            assert before.requires_approval, key
        elif has_plain_mark(row):
            # A dangerous or destructive operation keeps its level.
            assert row.safety_level == before.safety_level, key
            assert row.requires_approval is (row.safety_level == "destructive"), key
        else:
            assert (row.safety_level, row.requires_approval) == (
                before.safety_level,
                before.requires_approval,
            ), key
        # The code's own values never change.
        assert (row.code_safety_level, row.code_requires_approval) == (
            before.code_safety_level,
            before.code_requires_approval,
        ), key
    assert relevelled, "no registered operation has the plain mark: the test checks nothing"


def _imported(level: str, op_id: str) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="plainmark",
        version="1.0",
        impl_id="plainmark-rest",
        op_id=op_id,
        method="POST",
        path=f"/api/{op_id}",
        source_kind="ingested",
        safety_level=level,
        requires_approval=True,
        parameter_schema={"type": "object"},
        tags=[],
        tenant_id=_TENANT,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["safe", "caution"])
async def test_an_imported_plain_marked_op_counts_as_dangerous(
    monkeypatch: pytest.MonkeyPatch, level: str
) -> None:
    async with get_sessionmaker()() as session, session.begin():
        session.add(_imported(level, f"POST:/api/widgets/{level}"))

    for on, expected in ((False, (level, True)), (True, ("dangerous", False))):
        _switch(monkeypatch, on=on)
        async with get_sessionmaker()() as session:
            row = (
                await session.execute(
                    select(EndpointDescriptor).where(EndpointDescriptor.product == "plainmark")
                )
            ).scalar_one()
            assert plain_mark_counts_as_level(row) is on
            row = await apply_effective_policy(session, tenant_id=_TENANT, row=row)
        assert (row.safety_level, row.requires_approval) == expected


# ---------------------------------------------------------------------------
# The gate, for every operation and each kind of caller
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Caller:
    name: str
    operator: Operator
    acting_agent: str | None = None


def _operator(sub: str, kind: PrincipalKind, **fields: Any) -> Operator:
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        tenant_role=fields.pop("tenant_role", TenantRole.OPERATOR),
        principal_kind=kind,
        acr="1",
        auth_time=int(time.time()),
        **fields,
    )


def _callers() -> tuple[_Caller, ...]:
    user = PrincipalKind.USER
    return (
        _Caller("person in the console", _operator("person-web", user, web_session=True)),
        _Caller("person's token", _operator("person-token", user, token_checked=True)),
        _Caller("agent, no grant", _operator("agent-none", PrincipalKind.AGENT)),
        _Caller("agent, grant runs all", _operator(_AGENT, PrincipalKind.AGENT)),
        _Caller("service, no grant", _operator("svc-none", PrincipalKind.SERVICE)),
        _Caller("service, grant", _operator(_SERVICE, PrincipalKind.SERVICE)),
        _Caller(
            "hosted agent run",
            _operator("person-hosted", user, web_session=True),
            acting_agent=_AGENT,
        ),
        _Caller("MEHO's own login", _operator("meho-system", user, in_code_kind="system")),
    )


async def _seed_grants(rows: list[Any]) -> None:
    """An agent grant that runs every call, and a standing grant of the service per operation."""
    async with get_sessionmaker()() as session, session.begin():
        session.add(
            AgentPermission(
                tenant_id=_TENANT,
                principal_sub=_AGENT,
                op_pattern="*",
                verdict=PermissionVerdict.AUTO_EXECUTE,
                created_by_sub="test-admin",
            )
        )
        session.add_all(
            ServicePrincipalGrant(
                tenant_id=_TENANT,
                principal_sub=_SERVICE,
                op_id=row.op_id,
                connector_id=connector_id_of(row),
                target_id=None,
                reason="test: every operation",
                created_by_sub="test-admin",
            )
            for row in rows
        )


_Key = tuple[str, str, str]


async def _verdicts(
    rows: list[Any], *, evaluator: bool = False
) -> tuple[dict[_Key, PermissionVerdict], dict[_Key, Verdict]]:
    """The gate's answer for each caller and row, and the evaluator's with *evaluator*."""
    gate, evaluated = {}, {}
    for caller in _callers():
        delegation = actor_delegation(caller.acting_agent) if caller.acting_agent else nullcontext()
        with delegation:
            for row in rows:
                ask = {
                    "operator": caller.operator,
                    "descriptor": row,
                    "target": None,
                    "connector_id": connector_id_of(row),
                    "decide_only": True,
                }
                key = (caller.name, *_key(row))
                gate[key] = (await policy_gate(**ask))[0]
                if evaluator:
                    evaluated[key] = (await evaluate_call(**ask)).verdict
    return gate, evaluated


@pytest.mark.asyncio
async def test_no_call_waits_less_and_only_hard_to_undo_waits_more(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await register_all_descriptors()
    off_rows = await _effective_rows(monkeypatch, on=False)
    await _seed_grants(off_rows)
    _switch(monkeypatch, on=False)
    off, off_evaluated = await _verdicts(off_rows, evaluator=True)
    on_rows = await _effective_rows(monkeypatch, on=True)
    on, evaluated = await _verdicts(on_rows, evaluator=True)
    hard_to_undo = {_key(r) for r in on_rows if r.hard_to_undo}

    assert off.keys() == on.keys()
    looser = [k for k in off if _RANK[on[k]] < _RANK[off[k]]]
    stricter = [k for k in off if _RANK[on[k]] > _RANK[off[k]]]
    assert looser == [], f"{len(looser)} call(s) wait less with the switch on: {looser[:20]}"
    other = [k for k in stricter if k[1:] not in hard_to_undo]
    assert other == [], f"stricter without hard_to_undo: {other[:20]}"
    relevelled = {_key(r) for r in on_rows if r.safety_level == "dangerous"} - {
        _key(r) for r in off_rows if r.safety_level == "dangerous"
    }
    assert relevelled, "no operation was re-levelled: the comparison checks nothing new"
    # The evaluator gets the same rule: the switch adds no difference to the gate.
    differ_off = {k for k in off if _TO_EVALUATOR[off[k]] is not off_evaluated[k]}
    differ_on = {k for k in on if _TO_EVALUATOR[on[k]] is not evaluated[k]}
    assert sorted(differ_on - differ_off) == []


# ---------------------------------------------------------------------------
# A step of a multi-step operation, and the dry run of a policy edit
# ---------------------------------------------------------------------------


def _step(op_id: str, **fields: Any) -> EndpointDescriptor:
    return EndpointDescriptor(
        product="plainmark",
        version="1.0",
        impl_id="plainmark-api",
        op_id=op_id,
        source_kind="typed",
        safety_level="caution",
        requires_approval=True,
        parameter_schema={"type": "object"},
        tags=[],
        **fields,
    )


_NO_ENTRY = Classification(
    category=None, labels={}, risk=None, always_wait=False, rule_ref=DEFAULT_RULE_REF
)


@pytest.mark.asyncio
@pytest.mark.parametrize("on", [False, True], ids=["switch-off", "switch-on"])
async def test_a_step_and_the_dry_run_use_the_same_rule(
    monkeypatch: pytest.MonkeyPatch, on: bool
) -> None:
    _switch(monkeypatch, on=on)
    expected = ("dangerous", False) if on else ("caution", True)

    step = await apply_step_policy(tenant_id=_TENANT, step=_step("plainmark.widget.tune"))
    assert (step.safety_level, step.requires_approval) == expected
    assert (step.code_safety_level, step.code_requires_approval) == ("caution", True)
    policy = policy_in_force(_step("plainmark.widget.tune"), [], _NO_ENTRY)
    assert (policy.safety_level, policy.requires_approval) == expected

    # hard_to_undo is a delete sign: the step keeps its level and its mark.
    kept = await apply_step_policy(
        tenant_id=_TENANT, step=_step("plainmark.widget.wipe", hard_to_undo="deletes")
    )
    assert (kept.safety_level, kept.requires_approval) == ("caution", True)
