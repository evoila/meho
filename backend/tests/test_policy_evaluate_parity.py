# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parity test: the policy evaluator gives today's answer in every case.

The evaluator (``policy/evaluate.py``, ``policy/approve.py``) must give
the same answers as today's checks before any policy switch becomes the
default. This file runs every case through both and fails on any
difference. It prints each differing case with both answers.

* **Calls.** 6 caller kinds x 6 logins (the five login groups, and a
  platform admin in another tenant) x 4 risk levels x 32 combinations of
  five marks (requires approval, write, credential read, caller address,
  role floor) x with and without a grant: 9,216 cases. Today's side is
  ``policy_gate``; the evaluator's side is ``evaluate_call``, with the
  same lookups. Both must give the same verdict, the same reason class,
  the same approver groups and the same "waits only for its token" mark.
* **Approvals.** 5 approver kinds x 6 logins x own change or not x 5
  risk levels (also none) x token wait x requires approval x role floor x
  pending or decided x rights change x the break-glass switch: 19,200
  cases. Today's side is ``approve_right`` (the checks of
  ``approval_queue.py``); the evaluator's side is ``may_approve``.
* **Routes.** Every REST route with a ``require_role`` check and every
  MCP tool, for each of the six logins: the door answers the same as the
  evaluator's role floor for the login the evaluator reads.

The round trip of the shipped "small team" file (its export on a fresh
install equals the file, and importing it changes no answer of any
registered operation) is in ``test_policy_small_team_parity.py``.

How to read a failing case: ``docs/codebase/policy-evaluator.md``.
"""

from __future__ import annotations

import inspect
import itertools
import uuid
from collections.abc import Iterator
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from meho_backplane.auth.delegation import actor_delegation
from meho_backplane.auth.operator import Operator, PrincipalKind, RoleGroup, TenantRole, group_grant
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import (
    AgentPermission,
    ApprovalRequest,
    EndpointDescriptor,
    PermissionVerdict,
    ServicePrincipalGrant,
)
from meho_backplane.mcp import registry as mcp_registry
from meho_backplane.operations import role_floors
from meho_backplane.operations._validate import policy_gate, waits_only_for_token_call
from meho_backplane.operations.approval_rights import ApproveRightReason, approve_right
from meho_backplane.operations.caller_address import CALLER_ADDRESS_MARKER
from meho_backplane.policy.approve import may_approve
from meho_backplane.policy.evaluate import TOKEN_CALL_WAIT, Asked, Decision, Verdict
from meho_backplane.policy.floors import call_floors
from meho_backplane.policy.shadow import approval_ask_of, asker_of, evaluate_call
from meho_backplane.settings import get_settings
from tests._route_tree_helpers import iter_routes

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006043")
_CONNECTOR = "parity-rest-1.0"

_CALLERS = ("person_web", "person_token", "agent", "read_only_agent", "service", "hosted_run")
_LOGINS = (*(group.value for group in RoleGroup), "platform_admin_elsewhere")
_LEVELS = ("safe", "caution", "dangerous", "destructive")
_MARKS = ("requires_approval", "write", "credential_read", "caller_address", "role_floor")
_MASKS = range(2 ** len(_MARKS))

_FROM_TODAY = {
    PermissionVerdict.AUTO_EXECUTE: Verdict.RUN,
    PermissionVerdict.NEEDS_APPROVAL: Verdict.WAIT,
    PermissionVerdict.DENY: Verdict.DENY,
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("SERVICE_GRANT_DELETE_SHAPED_PATTERNS", raising=False)
    monkeypatch.setenv("POLICY_ENGINE_SHADOW", "false")
    monkeypatch.setenv("POLICY_APPROVER_ROUTING", "false")
    monkeypatch.setenv("AGENT_GRANT_REQUIRED", "off")
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _role_floor_ops(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operations with the role floor mark need the ``tenant_admin`` role."""
    for mask in _MASKS:
        if "role_floor" in _marks(mask):
            monkeypatch.setitem(role_floors._FLOORS, _op_id(mask), TenantRole.TENANT_ADMIN)


# ---------------------------------------------------------------------------
# Who asks
# ---------------------------------------------------------------------------


def _login_fields(login: str) -> dict[str, Any]:
    """The role and flags a login gives, as the token reader sets them."""
    if login == "platform_admin_elsewhere":
        # A platform admin who works in a tenant without a role group there.
        return {
            "tenant_role": TenantRole.TENANT_ADMIN,
            "platform_admin": True,
            "role_from_platform_admin": True,
        }
    grant = group_grant(RoleGroup(login))
    return {
        "tenant_role": grant.role,
        "approver": grant.approver,
        "self_approver": grant.self_approval,
    }


_KINDS = {
    "agent": PrincipalKind.AGENT,
    "read_only_agent": PrincipalKind.AGENT,
    "service": PrincipalKind.SERVICE,
    "runner": PrincipalKind.RUNNER,
}


def _operator(caller: str, login: str, sub: str) -> Operator:
    fields = _login_fields(login)
    kind = _KINDS.get(caller, PrincipalKind.USER)
    if kind is PrincipalKind.AGENT and fields["tenant_role"] is TenantRole.TENANT_ADMIN:
        # An agent token acts with at most the operator role.
        fields["tenant_role"] = TenantRole.OPERATOR
        fields["agent_role_capped"] = True
    if caller == "read_only_agent":
        fields["tenant_role"] = TenantRole.READ_ONLY
    return Operator(
        sub=sub,
        raw_jwt="header.payload.signature",
        tenant_id=_TENANT,
        principal_kind=kind,
        web_session=caller == "person_web",
        **fields,
    )


# ---------------------------------------------------------------------------
# What is asked
# ---------------------------------------------------------------------------


def _marks(mask: int) -> frozenset[str]:
    return frozenset(mark for bit, mark in enumerate(_MARKS) if mask >> bit & 1)


def _op_id(mask: int) -> str:
    return f"parity.op{mask:02d}"


def _descriptor(level: str, mask: int) -> EndpointDescriptor:
    marks = _marks(mask)
    schema: dict[str, Any] = {}
    if "caller_address" in marks:
        schema = {
            "type": "object",
            "properties": {"address": {"type": "string", CALLER_ADDRESS_MARKER: True}},
        }
    return EndpointDescriptor(
        product="parity",
        version="1.0",
        impl_id="parity-rest",
        op_id=_op_id(mask),
        source_kind="ingested",
        method="POST" if "write" in marks else "GET",
        tags=[],
        safety_level=level,
        requires_approval="requires_approval" in marks,
        op_class="credential_read" if "credential_read" in marks else None,
        parameter_schema=schema,
    )


# ---------------------------------------------------------------------------
# Calls
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Call:
    caller: str
    login: str
    grant: bool

    @property
    def sub(self) -> str:
        return f"{self.caller}:{self.login}:{'grant' if self.grant else 'none'}"

    @property
    def actor(self) -> str | None:
        """The hosted agent of a hosted run; it holds the grant."""
        return f"hosted-agent:{self.login}:{self.grant}" if self.caller == "hosted_run" else None


_CALLS = tuple(_Call(*cell) for cell in itertools.product(_CALLERS, _LOGINS, (False, True)))


async def _seed_grants() -> None:
    """The grant of each call with a grant: an agent grant, or a standing grant per operation."""
    async with get_sessionmaker()() as session, session.begin():
        for call in _CALLS:
            if not call.grant:
                continue
            agent_sub = call.actor or call.sub
            if call.caller in ("agent", "read_only_agent", "hosted_run"):
                session.add(
                    AgentPermission(
                        tenant_id=_TENANT,
                        principal_sub=agent_sub,
                        op_pattern="parity.*",
                        verdict=PermissionVerdict.AUTO_EXECUTE,
                        created_by_sub="test-admin",
                    )
                )
                continue
            for mask in _MASKS:
                session.add(
                    ServicePrincipalGrant(
                        tenant_id=_TENANT,
                        principal_sub=call.sub,
                        op_id=_op_id(mask),
                        connector_id=_CONNECTOR,
                        target_id=None,
                        reason="test: every operation",
                        created_by_sub="test-admin",
                    )
                )


def _today_class(verdict: PermissionVerdict, reason: str | None) -> str:
    """The reason class of today's answer: what let the call run, or what refused it."""
    text = reason or ""
    if verdict is PermissionVerdict.NEEDS_APPROVAL:
        return "wait"
    if verdict is PermissionVerdict.AUTO_EXECUTE:
        if "standing grant" in text or "delete grant" in text:
            return "standing_grant"
        if "run approval" in text:
            return "run_approval"
        if "permission row" in text or "tied rows" in text:
            return "agent_grant"
        return "default"
    if "role; your role is" in text:
        return "role_floor"
    if "agent_no_grant" in text:
        return "agent_no_grant"
    if "read scope" in text or "read_scope" in text:
        return "read_scope"
    return "agent_rule"


def _evaluator_class(decision: Decision) -> str:
    rule = decision.rule
    if decision.verdict is Verdict.WAIT:
        return "wait"
    if decision.verdict is Verdict.RUN:
        if rule.source in ("standing_grant", "run_approval", "agent_grant"):
            return rule.source
        return "default"
    if rule.source in ("role_floor", "read_scope"):
        return rule.source
    return "agent_no_grant" if rule.rule_id == "agent_no_grant" else "agent_rule"


def _today_approvers(level: str, op_id: str) -> frozenset[str]:
    """The login groups that may approve another person's call today (``approve_right``)."""
    request = _request(requester="someone-else", op_id=op_id, level=level)
    return frozenset(
        f"MEHO-{group.value}"
        for group in RoleGroup
        if approve_right(_operator("person_web", group.value, "approver"), request).may_approve
    )


async def _answers(call: _Call, level: str, mask: int) -> dict[str, Any]:
    """Today's answer and the evaluator's answer for one case."""
    operator = _operator(call.caller, call.login, call.sub)
    descriptor = _descriptor(level, mask)
    with actor_delegation(call.actor) if call.actor else nullcontext():
        verdict, reason = await policy_gate(
            operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
        )
        token_wait = waits_only_for_token_call(operator, descriptor)
        decision = await evaluate_call(
            operator=operator, descriptor=descriptor, target=None, connector_id=_CONNECTOR
        )
    waits = verdict is PermissionVerdict.NEEDS_APPROVAL
    approvers = _today_approvers(level, descriptor.op_id) if waits else frozenset()
    today = {
        "verdict": _FROM_TODAY[verdict].value,
        "class": _today_class(verdict, reason),
        "approvers": sorted(approvers),
        "waits_because": TOKEN_CALL_WAIT if waits and token_wait else None,
    }
    evaluator = {
        "verdict": decision.verdict.value,
        "class": _evaluator_class(decision),
        "approvers": sorted(decision.approvers),
        "waits_because": decision.waits_because,
    }
    return {"today": today, "today_reason": reason, "evaluator": evaluator, "trace": decision.trace}


def _case_text(call: _Call, level: str, mask: int, answers: dict[str, Any]) -> str:
    marks = ",".join(sorted(_marks(mask))) or "no marks"
    return (
        f"caller={call.caller} login={call.login} grant={call.grant} level={level} "
        f"marks=[{marks}]\n  today={answers['today']} reason={answers['today_reason']!r}\n"
        f"  evaluator={answers['evaluator']} trace={list(answers['trace'])}"
    )


@pytest.mark.asyncio
async def test_every_call_gets_todays_answer() -> None:
    await _seed_grants()
    cases = 0
    differences: list[str] = []
    seen: set[str] = set()
    for call, level, mask in itertools.product(_CALLS, _LEVELS, _MASKS):
        answers = await _answers(call, level, mask)
        cases += 1
        seen.add(f"{answers['today']['verdict']}/{answers['today']['class']}")
        if answers["today"] != answers["evaluator"]:
            differences.append(_case_text(call, level, mask, answers))
    assert cases == 6 * 6 * 4 * 32 * 2
    assert not differences, f"{len(differences)} of {cases} cases differ:\n" + "\n".join(
        differences[:20]
    )
    # The table reaches every kind of answer, so a check that never runs cannot pass.
    assert {"run/default", "run/standing_grant", "run/agent_grant", "wait/wait"} <= seen
    assert {"deny/role_floor"} <= seen


# ---------------------------------------------------------------------------
# Approvals
# ---------------------------------------------------------------------------


_APPROVER_KINDS = ("person_web", "person_token", "agent", "service", "runner")
_REQUEST_LEVELS = (*_LEVELS, None)

#: The evaluator's refusal rule and today's refusal reason that mean the same.
_REFUSALS = {
    "only_a_person_approves": ApproveRightReason.NOT_A_PERSON,
    "no_approval_access": ApproveRightReason.NO_APPROVAL_ACCESS,
    "not_pending": ApproveRightReason.NOT_PENDING,
    "rights_change": ApproveRightReason.RIGHTS_CHANGE,
    "grant_change": ApproveRightReason.ROLE_TOO_LOW,
    "own_change": ApproveRightReason.OWN_CHANGE,
    "wrong_group": ApproveRightReason.WRONG_GROUP,
    "role_floor": ApproveRightReason.ROLE_TOO_LOW,
}


def _request(
    *,
    requester: str,
    op_id: str,
    level: str | None,
    token_wait: bool = False,
    pending: bool = True,
    rights_change: bool = False,
) -> ApprovalRequest:
    effect: dict[str, Any] = {}
    if level is not None:
        effect["safety_level"] = level
    if token_wait:
        effect["waits_because"] = TOKEN_CALL_WAIT
    return ApprovalRequest(
        id=uuid.uuid4(),
        tenant_id=_TENANT,
        principal_sub=requester,
        op_id=op_id,
        status="pending" if pending else "approved",
        proposed_effect=effect,
        rights_change=rights_change,
    )


def _approval_differences() -> tuple[int, list[str]]:
    cases = 0
    differences: list[str] = []
    product = itertools.product(
        _APPROVER_KINDS,
        _LOGINS,
        (False, True),  # own change
        _REQUEST_LEVELS,
        (False, True),  # token wait
        (False, True),  # requires approval
        (False, True),  # role floor
        (True, False),  # pending
        (False, True),  # rights change
    )
    for kind, login, own, level, token, ra, floor, pending, rights in product:
        approver = _operator(kind, login, f"approver:{kind}:{login}")
        request = _request(
            requester=approver.sub if own else "someone-else",
            op_id=_op_id(16 if floor else 0),
            level=level,
            # The dispatcher stamps the token wait only on a call without requires approval.
            token_wait=token and not ra,
            pending=pending,
            rights_change=rights,
        )
        today = approve_right(approver, request)
        allowed, rule = may_approve(asker_of(approver), approval_ask_of(approver, request))
        cases += 1
        if allowed:
            same = today.may_approve and today.rule is not None and today.rule.value == rule.rule_id
        else:
            same = not today.may_approve and _REFUSALS.get(rule.rule_id) is today.reason
        if not same:
            differences.append(
                f"approver={kind} login={login} own={own} level={level} token_wait={token} "
                f"requires_approval={ra} role_floor={floor} pending={pending} "
                f"rights_change={rights}\n  today={today.may_approve} {today.reason.value} "
                f"rule={today.rule}\n  evaluator={allowed} {rule.source}:{rule.rule_id}"
            )
    return cases, differences


@pytest.mark.parametrize("break_glass", [False, True])
def test_every_approval_gets_todays_answer(
    monkeypatch: pytest.MonkeyPatch, break_glass: bool
) -> None:
    monkeypatch.setenv("APPROVAL_ALLOW_SELF_APPROVAL", "true" if break_glass else "false")
    get_settings.cache_clear()
    cases, differences = _approval_differences()
    assert cases == 5 * 6 * 2 * 5 * 2 * 2 * 2 * 2 * 2
    assert not differences, f"{len(differences)} of {cases} cases differ:\n" + "\n".join(
        differences[:20]
    )


# ---------------------------------------------------------------------------
# Routes and tools
# ---------------------------------------------------------------------------


def _calls(dependant: Dependant) -> Iterator[Any]:
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def _role_checks() -> list[tuple[str, TenantRole, Any]]:
    """Every ``require_role`` check of every REST route: (route, role, check)."""
    from meho_backplane.main import app

    checks: list[tuple[str, TenantRole, Any]] = []
    for route in iter_routes(app.routes):
        if not isinstance(route, APIRoute):
            continue
        for call in _calls(route.dependant):
            if not inspect.isfunction(call):
                continue
            min_role = inspect.getclosurevars(call).nonlocals.get("min_role")
            if isinstance(min_role, TenantRole):
                checks.append((f"{sorted(route.methods)} {route.path}", min_role, call))
    return checks


def _door_open(check: Any, operator: Operator) -> bool:
    try:
        check(operator)
    except HTTPException:
        return False
    return True


def _evaluator_allows(
    operator: Operator, role: TenantRole, monkeypatch: pytest.MonkeyPatch
) -> bool:
    """The evaluator's answer: its role floor for an operation that needs *role*."""
    probe = f"parity.door.{role.value}"
    monkeypatch.setitem(role_floors._FLOORS, probe, role)
    asked = Asked(connector_id=None, op_id=probe, risk="safe")
    return not any(hit.source == "role_floor" for hit in call_floors(asker_of(operator), asked))


def test_every_route_and_tool_role_check_agrees(monkeypatch: pytest.MonkeyPatch) -> None:
    checks = _role_checks()
    assert len(checks) > 100, "too few routes with a role check"
    mcp_registry.eager_import_mcp_modules()
    tools = [defn for defn, _handler in mcp_registry._TOOLS.values()]
    assert len(tools) > 50, "too few MCP tools"
    differences: list[str] = []
    cases = 0
    for login in _LOGINS:
        operator = _operator("person_token", login, f"person:{login}")
        asker = asker_of(operator)
        for name, role, check in checks:
            cases += 1
            if _door_open(check, operator) != _evaluator_allows(operator, role, monkeypatch):
                differences.append(f"login={login} route={name} needs={role.value}")
        for defn in tools:
            cases += 1
            evaluator = _evaluator_allows(operator, defn.required_role, monkeypatch) or (
                defn.approver_may_call and asker.kind == "person" and "approver" in asker.roles
            )
            if mcp_registry.tool_role_gate_met(operator, defn) != evaluator:
                differences.append(f"login={login} tool={defn.name} needs={defn.required_role}")
    assert not differences, f"{len(differences)} of {cases} cases differ:\n" + "\n".join(
        differences[:20]
    )
