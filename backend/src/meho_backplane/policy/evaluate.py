# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The policy evaluator: one function that says run, wait or deny for a call.

:func:`evaluate` takes who asks (:class:`Asker`), what is asked
(:class:`Asked`) and what MEHO found in the database for this call
(:class:`Lookups`). It returns a :class:`Decision`: the verdict, the rule
that decided it (:class:`RuleRef`), who may approve a call that waits,
and a trace of every step.

The schema is fixed. There is no rule language. The order is fixed too:

1. **Floors** (:mod:`meho_backplane.policy.floors`), from the code. A
   floor sets the least strict answer; nothing after it can loosen it.
2. **Customer rules.** None in part 1 of the policy work.
3. **Shipped defaults**, the table MEHO has today: a person in the web
   interface, a person with a token, the service ladder, agents, and the
   read-only ceiling.
4. **Per-principal steps**, in today's order: agent grants, a hosted
   agent run (the stricter of the caller and the agent), the read scope
   of a service login, a standing grant or a run approval that clears a
   service login's wait (never a floor, except a sandbox delete grant).

Some steps stay in the dispatcher and are not here: the children of an
approved run, the wait of a secret reveal, the announce gate, the role
floor check when an approved call resumes, and the satellite ladder.

The function is pure: no database, no settings, no log line. The caller
does the lookups (:mod:`meho_backplane.policy.shadow`). While the
``POLICY_ENGINE_SHADOW`` switch is on, the evaluator runs next to today's
checks and decides nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from meho_backplane.auth.operator import TenantRole
from meho_backplane.operations.role_floors import role_floor_for
from meho_backplane.policy.floors import (
    KNOWN_NON_SAFE,
    FloorHit,
    call_floors,
    grant_may_clear,
    run_approval_may_clear,
)

__all__ = [
    "TOKEN_CALL_WAIT",
    "Asked",
    "Asker",
    "Decision",
    "Lookups",
    "RuleRef",
    "Verdict",
    "approver_groups",
    "evaluate",
]

AskerKind = Literal["person", "agent", "service", "runner"]
Via = Literal["web", "token"]
RuleSource = Literal[
    "floor",
    "tenant_rule",
    "default",
    "agent_grant",
    "standing_grant",
    "run_approval",
    "read_scope",
    "role_floor",
]

#: ``Decision.waits_because`` for a person's ``caution`` call that waits only
#: because it came with a token. The same value as
#: :data:`meho_backplane.operations._validate.TOKEN_CALL_WAIT`.
TOKEN_CALL_WAIT: Final[str] = "token_call"

_HIGH_RISK: Final[frozenset[str]] = frozenset({"dangerous", "destructive"})


class Verdict(StrEnum):
    """What happens with a call: it runs, it waits for a person, or it is denied."""

    RUN = "run"
    WAIT = "wait"
    DENY = "deny"

    @property
    def rank(self) -> int:
        """0 for run, 1 for wait, 2 for deny: a higher rank is stricter."""
        return _RANK[self]


_RANK: Final[dict[Verdict, int]] = {Verdict.RUN: 0, Verdict.WAIT: 1, Verdict.DENY: 2}


@dataclass(frozen=True)
class Asker:
    """Who asks.

    ``roles`` holds the role in the tenant (``tenant_admin``, ``operator``
    or ``read_only``) and the flags ``approver``, ``sa_operator`` and
    ``platform_admin`` when the login has them. ``via`` is ``web`` only
    for a person in the web interface. ``acting_agent`` is the agent of a
    hosted run that makes the call for this asker.
    """

    kind: AskerKind
    sub: str
    tenant_id: UUID
    roles: frozenset[str]
    via: Via = "token"
    acting_agent: str | None = None
    tenant_chosen: bool = True

    @property
    def tenant_role(self) -> TenantRole:
        """The role in the tenant; ``read_only`` when ``roles`` names none (fail closed)."""
        for role in (TenantRole.TENANT_ADMIN, TenantRole.OPERATOR):
            if role.value in self.roles:
                return role
        return TenantRole.READ_ONLY


@dataclass(frozen=True)
class Asked:
    """What is asked: one call of one operation.

    ``risk`` and ``always_wait`` are the effective values after overrides,
    classification and floors. ``op_class`` holds marks from the code:
    ``root_shell``, ``credential_read``, ``per_call_approval``,
    ``delete_shaped`` and ``classified_always_wait``. ``target`` is the
    target id, or ``None``.
    """

    connector_id: str | None
    op_id: str
    risk: str
    always_wait: bool = False
    mutating: bool = False
    caller_address: bool = False
    op_class: frozenset[str] = frozenset()
    group_key: str | None = None
    category: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    target: str | None = None


@dataclass(frozen=True)
class Lookups:
    """What the caller found in the database for this call. The evaluator never reads it itself.

    ``agent_grant`` is the verdict and source of the agent grant that
    covers the call of an agent asker, ``acting_agent_grant`` the same for
    the agent of a hosted run (:func:`~meho_backplane.auth.permissions.matched_grant`).
    ``standing_grant`` and ``run_approval`` are the ids of a live grant or
    run approval that covers a service login's call. ``read_scope_denial``
    is the refusal text of a service login's read scope. ``sandbox`` and
    ``agent_grant_required`` are the settings at the time of the call.
    """

    agent_grant: tuple[Verdict, str] | None = None
    acting_agent_grant: tuple[Verdict, str] | None = None
    standing_grant: str | None = None
    run_approval: str | None = None
    read_scope_denial: str | None = None
    sandbox: bool = False
    agent_grant_required: str = "off"


@dataclass(frozen=True)
class RuleRef:
    """The rule that decided. Written to audit rows and approval requests later.

    ``policy_version`` is the version of the imported policy file; ``None``
    while no file decides (part 1).
    """

    source: RuleSource
    rule_id: str
    text: str
    policy_version: int | None = None


@dataclass(frozen=True)
class Decision:
    """The answer of :func:`evaluate`.

    ``approvers`` names the groups that may approve, only when the call
    waits. ``waits_because`` is :data:`TOKEN_CALL_WAIT` when a person's
    ``caution`` call waits only because it came with a token.
    """

    verdict: Verdict
    rule: RuleRef
    approvers: tuple[str, ...] = ()
    waits_because: str | None = None
    trace: tuple[str, ...] = ()


def approver_groups(risk: str, op_id: str) -> tuple[str, ...]:
    """The groups that may approve another person's call of *op_id* at *risk*.

    Today's group rule: a ``dangerous`` or ``destructive`` change only a
    MEHO-SA-Operator or a MEHO-Admin; a lower one also a MEHO-Operator and
    a MEHO-Approver. An operation with the ``tenant_admin`` role floor only
    a MEHO-Admin.
    """
    if role_floor_for(op_id) is TenantRole.TENANT_ADMIN:
        return ("MEHO-Admins",)
    if risk in _HIGH_RISK:
        return ("MEHO-SA-Operators", "MEHO-Admins")
    return ("MEHO-Operators", "MEHO-SA-Operators", "MEHO-Admins", "MEHO-Approvers")


class _Steps:
    """The verdict so far, the rule that set it, and the trace."""

    def __init__(self, verdict: Verdict, rule: RuleRef) -> None:
        self.verdict = verdict
        self.rule = rule
        self.trace: list[str] = [f"{rule.source} {rule.rule_id}: {verdict.value}"]

    def tighten(self, verdict: Verdict, rule: RuleRef, *, on_tie: bool = False) -> None:
        """Make the answer *verdict* when it is stricter (or as strict, with *on_tie*)."""
        self.trace.append(f"{rule.source} {rule.rule_id}: {verdict.value}")
        if verdict.rank > self.verdict.rank or (on_tie and verdict.rank == self.verdict.rank):
            self.verdict, self.rule = verdict, rule

    def set(self, verdict: Verdict, rule: RuleRef) -> None:
        """Set the answer, also when it is looser (a grant that clears a wait)."""
        self.trace.append(f"{rule.source} {rule.rule_id}: {verdict.value}")
        self.verdict, self.rule = verdict, rule


def _floor_rule(hit: FloorHit) -> RuleRef:
    return RuleRef(source=hit.source, rule_id=hit.rule_id, text=hit.text)


def _default(rule_id: str, text: str) -> RuleRef:
    return RuleRef(source="default", rule_id=rule_id, text=text)


def _apply_floors(steps: _Steps, hits: list[FloorHit]) -> None:
    """Floors come first in the order, so a floor names itself on a tie."""
    for hit in hits:
        steps.tighten(Verdict(hit.verdict), _floor_rule(hit), on_tie=True)


def _agent_start(asked: Asked, grant: tuple[Verdict, str] | None, mode: str) -> _Steps:
    """The shipped default for an agent, replaced by its grant when one covers the call."""
    if asked.risk == "safe":
        default = Verdict.RUN
    elif asked.risk in KNOWN_NON_SAFE:
        default = Verdict.WAIT
    else:
        default = Verdict.DENY
    steps = _Steps(default, _default(f"agent.{asked.risk}", f"agent default for {asked.risk}"))
    if grant is not None:
        verdict, source = grant
        steps.set(verdict, RuleRef(source="agent_grant", rule_id="agent_grant", text=source))
    elif mode == "on":
        steps.set(
            Verdict.DENY,
            RuleRef(
                source="agent_grant",
                rule_id="agent_no_grant",
                text="no grant covers this agent call, and MEHO requires one",
            ),
        )
    return steps


def _agent(asker: Asker, asked: Asked, grant: tuple[Verdict, str] | None, mode: str) -> _Steps:
    steps = _agent_start(asked, grant, mode)
    _apply_floors(steps, call_floors(asker, asked))
    if asker.tenant_role is TenantRole.READ_ONLY:
        steps.tighten(
            Verdict.WAIT,
            _default("read_only_ceiling", "a read_only login's agent never runs a call at once"),
        )
    if asked.always_wait:
        steps.tighten(Verdict.WAIT, _default("always_wait", "the operation always waits"))
    return steps


def _service_ladder(asked: Asked) -> tuple[Verdict, RuleRef]:
    if asked.risk == "dangerous":
        return Verdict.WAIT, _default("service.dangerous", "a service login's dangerous call waits")
    if asked.risk == "caution" and asked.mutating:
        return Verdict.WAIT, _default(
            "service.caution_write", "a service login's caution write waits"
        )
    if "root_shell" in asked.op_class:
        return Verdict.WAIT, _default(
            "service.root_shell", "the operation runs a command as root on the host"
        )
    return Verdict.RUN, _default(f"service.{asked.risk}", "a service login runs this at once")


def _person_default(asker: Asker, asked: Asked) -> tuple[Verdict, RuleRef]:
    if asked.risk == "safe":
        return Verdict.RUN, _default("person.safe", "a safe call runs at once")
    if asker.kind == "person" and asker.via == "web" and asked.risk == "caution":
        return Verdict.RUN, _default("person.web.caution", "in the web interface caution runs")
    where = "web" if asker.kind == "person" and asker.via == "web" else "token"
    return Verdict.WAIT, _default(f"person.{where}.{asked.risk}", f"risk level {asked.risk} waits")


def _non_agent_default(asker: Asker, asked: Asked) -> _Steps:
    if asker.kind == "service":
        verdict, rule = _service_ladder(asked)
    else:
        verdict, rule = _person_default(asker, asked)
    steps = _Steps(verdict, rule)
    if asked.always_wait:
        steps.tighten(Verdict.WAIT, _default("always_wait", "the operation always waits"))
    return steps


def _service_clear(steps: _Steps, hits: list[FloorHit], asked: Asked, found: Lookups) -> None:
    """A standing grant or a run approval clears a service login's wait, never a floor."""
    floor_waits = [hit for hit in hits if hit.verdict == "wait"]
    if floor_waits:
        if (
            all(hit.sandbox_delete_grant_clears for hit in floor_waits)
            and found.sandbox
            and found.standing_grant is not None
            and grant_may_clear(asked, sandbox=True)
        ):
            steps.set(
                Verdict.RUN,
                RuleRef(
                    source="standing_grant",
                    rule_id="sandbox_delete_grant",
                    text=f"sandbox delete grant {found.standing_grant}",
                ),
            )
        return
    if found.standing_grant is not None and grant_may_clear(asked, sandbox=found.sandbox):
        steps.set(
            Verdict.RUN,
            RuleRef(
                source="standing_grant",
                rule_id="standing_grant",
                text=f"standing grant {found.standing_grant}",
            ),
        )
    elif found.run_approval is not None and run_approval_may_clear(asked):
        steps.set(
            Verdict.RUN,
            RuleRef(
                source="run_approval",
                rule_id="run_approval",
                text=f"run approval {found.run_approval}",
            ),
        )


def _non_agent(asker: Asker, asked: Asked, found: Lookups) -> _Steps:
    hits = call_floors(asker, asked)
    if hits and hits[0].verdict == "deny":
        return _Steps(Verdict.DENY, _floor_rule(hits[0]))
    steps = _non_agent_default(asker, asked)
    _apply_floors(steps, hits)
    if asker.kind != "service":
        return steps
    if steps.verdict is Verdict.RUN and found.read_scope_denial is not None:
        steps.set(
            Verdict.DENY,
            RuleRef(source="read_scope", rule_id="read_scope", text=found.read_scope_denial),
        )
    elif steps.verdict is Verdict.WAIT and asked.connector_id is not None:
        _service_clear(steps, hits, asked, found)
    return steps


def _for_caller(
    asker: Asker, asked: Asked, found: Lookups, grant: tuple[Verdict, str] | None
) -> _Steps:
    if asker.kind == "agent":
        return _agent(asker, asked, grant, found.agent_grant_required)
    return _non_agent(asker, asked, found)


def _hosted_run(asker: Asker, asked: Asked, found: Lookups) -> _Steps:
    """The stricter of the caller and the hosted agent, in today's order (#4237).

    A deny of the agent decides. When the agent waits, only an agent caller
    can still make it stricter. Otherwise the caller's verdict counts when
    it is as strict as the agent's.
    """
    agent_id = asker.acting_agent or ""
    agent_asker = replace(asker, kind="agent", sub=agent_id, acting_agent=None)
    agent = _for_caller(agent_asker, asked, found, found.acting_agent_grant)
    agent.rule = replace(
        agent.rule, text=f"agent {agent_id} acting for {asker.sub}: {agent.rule.text}"
    )
    if agent.verdict is Verdict.DENY or (agent.verdict is Verdict.WAIT and asker.kind != "agent"):
        return agent
    caller = _for_caller(replace(asker, acting_agent=None), asked, found, found.agent_grant)
    caller.trace = [*agent.trace, *caller.trace]
    return caller if caller.verdict.rank >= agent.verdict.rank else agent


def _waits_because(asker: Asker, asked: Asked, verdict: Verdict) -> str | None:
    token_wait = (
        verdict is Verdict.WAIT
        and asker.kind == "person"
        and asker.via == "token"
        and asker.acting_agent is None
        and asked.risk == "caution"
        and not asked.always_wait
    )
    return TOKEN_CALL_WAIT if token_wait else None


def evaluate(asker: Asker, asked: Asked, found: Lookups) -> Decision:
    """Return run, wait or deny for *asker* calling *asked*, and the rule that decided."""
    if asker.acting_agent is not None:
        steps = _hosted_run(asker, asked, found)
    else:
        steps = _for_caller(asker, asked, found, found.agent_grant)
    waiting = steps.verdict is Verdict.WAIT
    return Decision(
        verdict=steps.verdict,
        rule=steps.rule,
        approvers=approver_groups(asked.risk, asked.op_id) if waiting else (),
        waits_because=_waits_because(asker, asked, steps.verdict),
        trace=tuple(steps.trace),
    )
