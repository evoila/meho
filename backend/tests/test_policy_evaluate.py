# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The policy evaluator (:mod:`meho_backplane.policy.evaluate`) and its order.

Pure tests: no database. They pin the order floors, then shipped defaults,
then per-principal steps, and that no later step loosens a floor.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import pytest

from meho_backplane.auth.operator import TenantRole
from meho_backplane.operations.role_floors import _FLOORS, register_role_floor
from meho_backplane.policy.approve import ApprovalAsk, may_approve
from meho_backplane.policy.evaluate import (
    TOKEN_CALL_WAIT,
    Asked,
    Asker,
    Lookups,
    Verdict,
    approver_groups,
    evaluate,
)

_TENANT = uuid.UUID("00000000-0000-0000-0000-000000006042")
_FLOOR_OP = "test.policy.admin_only"


@pytest.fixture(autouse=True)
def _role_floor() -> Iterator[None]:
    register_role_floor(_FLOOR_OP, TenantRole.TENANT_ADMIN)
    yield
    _FLOORS.pop(_FLOOR_OP, None)


def _asker(kind: str = "person", *, via: str = "token", role: str = "operator", **kw: Any) -> Asker:
    roles = frozenset({role, *kw.pop("flags", ())})
    sub = kw.pop("sub", f"{kind}-1")
    fields: dict[str, Any] = {"kind": kind, "via": via, **kw}
    return Asker(sub=sub, tenant_id=_TENANT, roles=roles, **fields)


def _asked(risk: str = "safe", **kw: Any) -> Asked:
    return Asked(
        connector_id=kw.pop("connector_id", "c-1"), op_id=kw.pop("op_id", "x.op"), risk=risk, **kw
    )


# ---------------------------------------------------------------------------
# Floors come first and are never loosened
# ---------------------------------------------------------------------------


def test_role_floor_denies_before_everything() -> None:
    decision = evaluate(
        _asker(via="web"), _asked("safe", op_id=_FLOOR_OP, always_wait=True), Lookups()
    )
    assert decision.verdict is Verdict.DENY
    assert decision.rule.source == "role_floor"
    assert decision.trace == ("role_floor role_floor: deny",)


def test_role_floor_lets_an_admin_through() -> None:
    decision = evaluate(_asker(role="tenant_admin"), _asked("safe", op_id=_FLOOR_OP), Lookups())
    assert decision.verdict is Verdict.RUN


def test_destructive_floor_names_itself() -> None:
    decision = evaluate(_asker(via="web"), _asked("destructive"), Lookups())
    assert decision.verdict is Verdict.WAIT
    assert (decision.rule.source, decision.rule.rule_id) == ("floor", "destructive_waits")


def test_no_grant_loosens_a_destructive_floor_outside_sandbox() -> None:
    found = Lookups(standing_grant="g-1", run_approval="r-1")
    decision = evaluate(_asker("service"), _asked("destructive"), found)
    assert decision.verdict is Verdict.WAIT


def test_a_sandbox_delete_grant_clears_a_service_destructive_wait() -> None:
    found = Lookups(standing_grant="g-1", sandbox=True)
    decision = evaluate(
        _asker("service"), _asked("destructive", op_class=frozenset({"delete_shaped"})), found
    )
    assert decision.verdict is Verdict.RUN
    assert decision.rule.rule_id == "sandbox_delete_grant"


def test_an_agent_grant_never_lifts_the_token_floor() -> None:
    found = Lookups(agent_grant=(Verdict.RUN, "permission row"))
    decision = evaluate(_asker("agent"), _asked("caution"), found)
    assert decision.verdict is Verdict.WAIT
    assert decision.rule.rule_id == "token_runs_only_safe"


# ---------------------------------------------------------------------------
# Shipped defaults
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("via", "risk", "expected"),
    [
        ("web", "safe", Verdict.RUN),
        ("web", "caution", Verdict.RUN),
        ("web", "dangerous", Verdict.WAIT),
        ("web", "unknown", Verdict.WAIT),
        ("token", "safe", Verdict.RUN),
        ("token", "caution", Verdict.WAIT),
        ("token", "unknown", Verdict.WAIT),
    ],
)
def test_person_defaults(via: str, risk: str, expected: Verdict) -> None:
    assert evaluate(_asker(via=via), _asked(risk), Lookups()).verdict is expected


def test_a_person_token_caution_waits_because_of_the_token() -> None:
    decision = evaluate(_asker(), _asked("caution"), Lookups())
    assert decision.waits_because == TOKEN_CALL_WAIT
    assert evaluate(_asker(), _asked("caution", always_wait=True), Lookups()).waits_because is None
    assert evaluate(_asker(via="web"), _asked("caution"), Lookups()).waits_because is None


@pytest.mark.parametrize(
    ("risk", "mutating", "op_class", "expected"),
    [
        ("safe", False, frozenset(), Verdict.RUN),
        ("caution", False, frozenset(), Verdict.RUN),
        ("caution", True, frozenset(), Verdict.WAIT),
        ("dangerous", False, frozenset(), Verdict.WAIT),
        ("safe", False, frozenset({"root_shell"}), Verdict.WAIT),
        ("unknown", True, frozenset(), Verdict.RUN),
    ],
)
def test_service_ladder(
    risk: str, mutating: bool, op_class: frozenset[str], expected: Verdict
) -> None:
    asked = _asked(risk, mutating=mutating, op_class=op_class)
    assert evaluate(_asker("service"), asked, Lookups()).verdict is expected


def test_runner_is_a_token_caller() -> None:
    assert evaluate(_asker("runner"), _asked("safe"), Lookups()).verdict is Verdict.RUN
    assert evaluate(_asker("runner"), _asked("caution"), Lookups()).verdict is Verdict.WAIT


@pytest.mark.parametrize(
    ("risk", "expected"),
    [
        ("safe", Verdict.RUN),
        ("caution", Verdict.WAIT),
        ("destructive", Verdict.WAIT),
        ("odd", Verdict.DENY),
    ],
)
def test_agent_defaults(risk: str, expected: Verdict) -> None:
    assert evaluate(_asker("agent"), _asked(risk), Lookups()).verdict is expected


def test_read_only_ceiling_makes_an_agent_wait_also_on_safe() -> None:
    decision = evaluate(_asker("agent", role="read_only"), _asked("safe"), Lookups())
    assert decision.verdict is Verdict.WAIT
    assert decision.rule.rule_id == "read_only_ceiling"


def test_always_wait_floors_an_agent_grant() -> None:
    found = Lookups(agent_grant=(Verdict.RUN, "row"))
    assert (
        evaluate(_asker("agent"), _asked("safe", always_wait=True), found).verdict is Verdict.WAIT
    )


# ---------------------------------------------------------------------------
# Per-principal steps
# ---------------------------------------------------------------------------


def test_agent_grant_can_deny_and_can_run_a_safe_call() -> None:
    deny = Lookups(agent_grant=(Verdict.DENY, "row"))
    assert evaluate(_asker("agent"), _asked("safe"), deny).verdict is Verdict.DENY
    run = Lookups(agent_grant=(Verdict.RUN, "row"))
    assert evaluate(_asker("agent"), _asked("odd"), run).verdict is Verdict.RUN


def test_agent_grant_required_denies_an_agent_without_a_grant() -> None:
    decision = evaluate(_asker("agent"), _asked("safe"), Lookups(agent_grant_required="on"))
    assert decision.verdict is Verdict.DENY
    assert decision.rule.rule_id == "agent_no_grant"
    observe = Lookups(agent_grant_required="observe")
    assert evaluate(_asker("agent"), _asked("safe"), observe).verdict is Verdict.RUN


def test_read_scope_denies_a_service_read() -> None:
    found = Lookups(read_scope_denial="read_scope_denied: target not in scope")
    decision = evaluate(_asker("service"), _asked("safe"), found)
    assert decision.verdict is Verdict.DENY
    assert decision.rule.source == "read_scope"


def test_standing_grant_then_run_approval_clear_a_service_wait() -> None:
    asked = _asked("dangerous")
    grant = evaluate(_asker("service"), asked, Lookups(standing_grant="g-1", run_approval="r-1"))
    assert (grant.verdict, grant.rule.source) == (Verdict.RUN, "standing_grant")
    run = evaluate(_asker("service"), asked, Lookups(run_approval="r-1"))
    assert (run.verdict, run.rule.source) == (Verdict.RUN, "run_approval")


@pytest.mark.parametrize(
    "asked",
    [
        _asked("dangerous", caller_address=True),
        _asked("dangerous", op_class=frozenset({"credential_read"})),
        _asked("dangerous", op_class=frozenset({"classified_always_wait"})),
        _asked("dangerous", op_class=frozenset({"per_call_approval"})),
    ],
)
def test_no_grant_and_no_run_approval_clear_these(asked: Asked) -> None:
    found = Lookups(standing_grant="g-1", run_approval="r-1")
    assert evaluate(_asker("service"), asked, found).verdict is Verdict.WAIT


def test_a_run_approval_never_clears_a_root_shell_call() -> None:
    asked = _asked("safe", op_class=frozenset({"root_shell"}))
    assert evaluate(_asker("service"), asked, Lookups(run_approval="r-1")).verdict is Verdict.WAIT
    assert evaluate(_asker("service"), asked, Lookups(standing_grant="g-1")).verdict is Verdict.RUN


def test_no_connector_means_no_grant() -> None:
    asked = _asked("dangerous", connector_id=None)
    assert evaluate(_asker("service"), asked, Lookups(standing_grant="g-1")).verdict is Verdict.WAIT


def test_hosted_run_takes_the_stricter_answer() -> None:
    person = _asker(via="web", acting_agent="agent:helper")
    # The agent waits on caution; the person in the web would run it.
    decision = evaluate(person, _asked("caution"), Lookups())
    assert decision.verdict is Verdict.WAIT
    assert decision.rule.text.startswith("agent agent:helper acting for person-1")
    assert decision.waits_because is None
    # The agent's grant denies: the deny decides.
    deny = Lookups(acting_agent_grant=(Verdict.DENY, "row"))
    assert evaluate(person, _asked("safe"), deny).verdict is Verdict.DENY


def test_hosted_run_keeps_todays_skip_of_the_callers_role_floor() -> None:
    """When the agent waits and the caller is not an agent, today's gate stops there."""
    person = _asker(via="web", acting_agent="agent:helper")
    assert evaluate(person, _asked("caution", op_id=_FLOOR_OP), Lookups()).verdict is Verdict.WAIT
    assert evaluate(person, _asked("safe", op_id=_FLOOR_OP), Lookups()).verdict is Verdict.DENY


def test_hosted_run_of_an_agent_caller_uses_both_grants() -> None:
    agent = _asker("agent", acting_agent="agent:helper")
    found = Lookups(agent_grant=(Verdict.DENY, "caller row"))
    decision = evaluate(agent, _asked("caution"), found)
    assert decision.verdict is Verdict.DENY
    assert decision.rule.text == "caller row"


def test_approvers_only_for_a_wait() -> None:
    assert evaluate(_asker(), _asked("safe"), Lookups()).approvers == ()
    assert evaluate(_asker(), _asked("dangerous"), Lookups()).approvers == (
        "MEHO-SA-Operators",
        "MEHO-Admins",
    )
    assert approver_groups("caution", "x.op")[0] == "MEHO-Operators"
    assert approver_groups("caution", _FLOOR_OP) == ("MEHO-Admins",)


def test_unknown_role_falls_back_to_read_only() -> None:
    asker = Asker(kind="person", sub="p", tenant_id=_TENANT, roles=frozenset())
    assert asker.tenant_role is TenantRole.READ_ONLY


# ---------------------------------------------------------------------------
# may_approve
# ---------------------------------------------------------------------------


def _ask(**kw: Any) -> ApprovalAsk:
    return ApprovalAsk(
        requester_sub=kw.pop("requester_sub", "someone"),
        op_id=kw.pop("op_id", "x.op"),
        risk=kw.pop("risk", "caution"),
        **kw,
    )


@pytest.mark.parametrize(
    ("asker", "ask", "allowed", "rule_id"),
    [
        (_asker("agent"), _ask(), False, "only_a_person_approves"),
        (_asker("service", role="tenant_admin"), _ask(), False, "only_a_person_approves"),
        (_asker(role="read_only"), _ask(), False, "no_approval_access"),
        (_asker(role="read_only", flags={"approver"}), _ask(), True, "reviewer"),
        (_asker(), _ask(pending=False), False, "not_pending"),
        (_asker(), _ask(risk="dangerous"), False, "wrong_group"),
        (_asker(flags={"sa_operator"}), _ask(risk="destructive"), True, "sa_operator_or_admin"),
        (
            _asker(role="tenant_admin", tenant_chosen=False),
            _ask(risk="dangerous"),
            True,
            "sa_operator_or_admin",
        ),
        (_asker(), _ask(rights_change=True), False, "rights_change"),
        (_asker(role="tenant_admin"), _ask(rights_change=True), True, "reviewer"),
        (_asker(flags={"platform_admin"}), _ask(rights_change=True), True, "reviewer"),
        (_asker(), _ask(grant_refusal="for your own login"), False, "grant_change"),
        (_asker(role="tenant_admin"), _ask(op_id=_FLOOR_OP), True, "reviewer"),
        (_asker(), _ask(op_id=_FLOOR_OP), False, "role_floor"),
    ],
)
def test_may_approve_others(asker: Asker, ask: ApprovalAsk, allowed: bool, rule_id: str) -> None:
    got, rule = may_approve(asker, ask)
    assert (got, rule.rule_id) == (allowed, rule_id)


@pytest.mark.parametrize(
    ("asker", "ask", "allowed", "rule_id"),
    [
        (_asker(sub="me"), _ask(), False, "own_change"),
        (
            _asker(sub="me", flags={"sa_operator"}),
            _ask(risk="destructive"),
            True,
            "sa_operator_self_approval",
        ),
        (_asker(sub="me", role="tenant_admin"), _ask(), True, "sa_operator_self_approval"),
        (_asker(sub="me", role="tenant_admin", tenant_chosen=False), _ask(), False, "own_change"),
        (
            _asker(sub="me", via="web"),
            _ask(waits_because=TOKEN_CALL_WAIT),
            True,
            "token_call_self_approval",
        ),
        (_asker(sub="me"), _ask(waits_because=TOKEN_CALL_WAIT), False, "own_change"),
        (_asker(sub="me"), _ask(break_glass=True), True, "break_glass_self_approval"),
        (_asker(sub="me"), _ask(break_glass=True, risk="dangerous"), False, "own_change"),
        (_asker(sub="me", role="tenant_admin"), _ask(rights_change=True), False, "rights_change"),
    ],
)
def test_may_approve_own_change(
    asker: Asker, ask: ApprovalAsk, allowed: bool, rule_id: str
) -> None:
    got, rule = may_approve(asker, _ask_for("me", ask))
    assert (got, rule.rule_id) == (allowed, rule_id)


def _ask_for(requester: str, ask: ApprovalAsk) -> ApprovalAsk:
    return replace(ask, requester_sub=requester)
