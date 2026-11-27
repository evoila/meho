# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``agents`` part of a tenant's policy file: rules for personal agents.

A personal agent is a person's own login used by a tool (the command line,
REST, MCP), not from the web console and not a hosted agent run. A tenant
admin writes the rules in the tenant's file::

    agents:
      - name: admins-assistant
        caller: personal_agent
        roles: [Admin]                    # Reader, Operator, Admin
        up_to: dangerous                  # safe, caution or dangerous
        connectors: [vmware-rest-9.0]     # optional
        rights_changes: allow             # optional, only with connectors

The caller says whether the ``CUSTOM_POLICY`` switch is on
(``custom_policy``); this module never reads a setting.

* With the switch off, a file that holds the key ``agents`` is refused,
  whatever its value, with :data:`AGENTS_NEED_CUSTOM_POLICY`.
* The install's file never holds ``agents``: it sets only the outer limit
  (:mod:`meho_backplane.policy.limits`).
* ``caller: person`` and ``caller: service_agent`` are refused: "not in
  this version".
* A rule whose ``up_to`` is above the outer limit
  (:func:`~meho_backplane.policy.limits.personal_agent_limit`) is refused.
* ``rights_changes: allow`` lets a rule allow changes of rights in the
  target systems of its ``connectors``. A rule without ``connectors``
  never allows them.

How a rule is read (:func:`rule_for`): a rule that names the connector
wins over a rule without ``connectors``. Between rules that match equally,
the stricter one wins: the lower ``up_to``; with the same ``up_to``, the
rule that does not allow changes of rights. What counts is the lowest of
the rule, the outer limit and the role's own limit
(:data:`ROLE_LIMITS`): a person's agent never gets more than the person.

No call decision reads the rules in this version of MEHO. The import plan
shows what they would let run at once (:func:`runs_at_once_by_role`).
A stored part that MEHO cannot read gives no rule.

Pure functions; no database, no settings.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Final

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from meho_backplane.instance_settings.schema import format_validation_errors
from meho_backplane.policy.classification import RISK_LEVELS, Scope
from meho_backplane.policy.limits import LIMIT_LEVELS

__all__ = [
    "AGENTS_IN_INSTALL_FILE",
    "AGENTS_NEED_CUSTOM_POLICY",
    "ROLE_LIMITS",
    "RULE_ROLES",
    "AgentRule",
    "RuleOp",
    "agent_rules_mistakes",
    "agents_diff",
    "allowed_level",
    "rule_for",
    "rule_lets_run",
    "rule_ref",
    "rules_above_limit",
    "rules_from_document",
    "runs_at_once_by_role",
    "stored_agents",
]

#: The roles a rule can name. A platform admin counts as ``Admin``.
RULE_ROLES: Final[tuple[str, ...]] = ("Reader", "Operator", "Admin")

#: How far a personal agent may go by the person's role: as far as the person alone.
ROLE_LIMITS: Final[Mapping[str, str]] = {
    "Reader": "safe",
    "Operator": "caution",
    "Admin": "dangerous",
}

#: The callers a rule can be for in this version.
_CALLERS: Final[tuple[str, ...]] = ("personal_agent",)

#: The callers that later versions add.
_LATER_CALLERS: Final[tuple[str, ...]] = ("person", "service_agent")

_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")

#: The refusal of ``agents`` in a file while the switch is off.
AGENTS_NEED_CUSTOM_POLICY: Final[str] = (
    "`agents` needs CUSTOM_POLICY: personal agent rules count only while the CUSTOM_POLICY "
    "setting is on. Remove the part, or ask the person who runs MEHO to turn the setting on."
)

#: The refusal of ``agents`` in the install's file.
AGENTS_IN_INSTALL_FILE: Final[str] = (
    "`agents` belongs to a tenant's policy file. The install's file sets only the outer "
    "limit (`limits`); each tenant admin sets the rules of the tenant inside it."
)

_log = structlog.get_logger(__name__)


class PolicyAgentRule(BaseModel):
    """``agents[]``: one rule for personal agents."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    name: str
    caller: str
    roles: list[str] = Field(min_length=1, max_length=len(RULE_ROLES))
    up_to: str
    connectors: list[Annotated[str, Field(min_length=1, max_length=200)]] | None = Field(
        default=None, min_length=1, max_length=200
    )
    rights_changes: str | None = None

    @field_validator("name")
    @classmethod
    def _name_fits(cls, value: str) -> str:
        if not _NAME_RE.match(value):
            raise ValueError(
                "is not a rule name: use 1 to 63 lower case letters, digits, - and _, "
                "starting with a letter"
            )
        return value

    @field_validator("caller")
    @classmethod
    def _known_caller(cls, value: str) -> str:
        if value in _LATER_CALLERS:
            raise ValueError(
                f"is `{value}`, which is not in this version of MEHO. A rule is only for "
                "`personal_agent`: a person's own login used by a tool. Service agents keep "
                "their standing grants"
            )
        if value not in _CALLERS:
            raise ValueError("is not a kind of caller; use personal_agent")
        return value

    @field_validator("roles")
    @classmethod
    def _known_roles(cls, value: list[str]) -> list[str]:
        if any(role not in RULE_ROLES for role in value):
            raise ValueError(
                "names a role that does not exist; use "
                + ", ".join(RULE_ROLES)
                + " (a platform admin counts as Admin)"
            )
        if len(set(value)) != len(value):
            raise ValueError("names a role twice")
        return value

    @field_validator("up_to")
    @classmethod
    def _known_level(cls, value: str) -> str:
        if value not in LIMIT_LEVELS:
            raise ValueError(
                "is not a level a rule can allow; use safe, caution or dangerous. "
                "A destructive operation always waits"
            )
        return value

    @field_validator("connectors")
    @classmethod
    def _connectors_once(cls, value: list[str] | None) -> list[str] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("names a connector twice")
        return value

    @field_validator("rights_changes")
    @classmethod
    def _known_rights_changes(cls, value: str | None) -> str | None:
        if value is not None and value != "allow":
            raise ValueError("is not a value it can have; use allow, or leave the key out")
        return value

    @model_validator(mode="after")
    def _rights_need_connectors(self) -> PolicyAgentRule:
        if self.rights_changes is not None and self.connectors is None:
            raise ValueError(
                "`rights_changes` needs `connectors`: a rule without connectors never "
                "allows changes of rights"
            )
        return self


@dataclass(frozen=True)
class AgentRule:
    """One readable rule. ``connectors`` is empty for a rule of every connector."""

    name: str
    roles: frozenset[str]
    up_to: str
    connectors: frozenset[str]
    rights_changes: bool


def rule_ref(rule: AgentRule) -> str:
    """The rule id that names *rule* in a plan, an explain answer or an audit row."""
    return f"tenant_rule:{rule.name}"


def _parse(raw: Any) -> tuple[list[PolicyAgentRule], list[str]]:
    """The rules of *raw* (the value of ``agents``) and one sentence per mistake."""
    if raw is None:
        return [], []
    if not isinstance(raw, list):
        return [], ["`agents` must be a list of rules."]
    rules: list[PolicyAgentRule] = []
    mistakes: list[str] = []
    for index, item in enumerate(raw):
        try:
            rules.append(PolicyAgentRule.model_validate(item))
        except ValidationError as exc:
            mistakes.extend(format_validation_errors(exc, ("agents", index)))
    if mistakes:
        return [], mistakes
    seen: dict[str, int] = {}
    for index, rule in enumerate(rules):
        if rule.name in seen:
            mistakes.append(
                f"`agents[{index}].name` repeats `agents[{seen[rule.name]}]`; give each "
                "rule its own name."
            )
        else:
            seen[rule.name] = index
    return ([], mistakes) if mistakes else (rules, [])


def stored_agents(raw: Any) -> Any:
    """The stored form of the value of ``agents``: only the keys that are set.

    A value with a mistake stays as written; :func:`agent_rules_mistakes`
    refuses it before anything is stored. ``null`` is stored as ``[]``, so
    the checks still see the key.
    """
    rules, mistakes = _parse(raw)
    if mistakes:
        return raw
    return [rule.model_dump(exclude_none=True) for rule in rules]


def agent_rules_mistakes(
    document: Mapping[str, Any], *, scope: Scope, custom_policy: bool, limit: str
) -> list[str]:
    """The mistakes of the ``agents`` part of *document*, a valid file's stored form.

    *limit* is the outer limit
    (:func:`~meho_backplane.policy.limits.personal_agent_limit`). The check
    reads whether the file has the key ``agents``, not whether the part has
    a value.
    """
    if "agents" not in document:
        return []
    if not custom_policy:
        return [AGENTS_NEED_CUSTOM_POLICY]
    if scope == "install":
        return [AGENTS_IN_INSTALL_FILE]
    rules, mistakes = _parse(document["agents"])
    if mistakes:
        return mistakes
    return [
        f"`agents[{index}].up_to` is above the outer limit for personal agents: the rule "
        f"`{rule.name}` allows {rule.up_to}, the limit is {limit}. Only a platform admin "
        "raises the limit, in the install's policy file."
        for index, rule in enumerate(rules)
        if LIMIT_LEVELS.index(rule.up_to) > LIMIT_LEVELS.index(limit)
    ]


def _readable(rule: PolicyAgentRule) -> AgentRule:
    return AgentRule(
        name=rule.name,
        roles=frozenset(rule.roles),
        up_to=rule.up_to,
        connectors=frozenset(rule.connectors or ()),
        rights_changes=rule.rights_changes == "allow",
    )


def rules_from_document(document: Any, *, place: str = "tenant:agents") -> list[AgentRule]:
    """The rules of a stored tenant *document*. A part MEHO cannot read gives no rule.

    Such a part gets one warning that names *place*. The outer limit is
    not applied here: :func:`allowed_level` applies it.
    """
    raw = document.get("agents") if isinstance(document, Mapping) else None
    rules, mistakes = _parse(raw)
    if mistakes:
        _log.warning(
            "policy_agent_rules_not_readable",
            place=place,
            message=(
                f"MEHO cannot read the stored rules at {place}, so no rule counts. "
                "Import the tenant's policy file again."
            ),
        )
        return []
    return [_readable(rule) for rule in rules]


def _strictness(rule: AgentRule) -> tuple[int, bool]:
    return LIMIT_LEVELS.index(rule.up_to), rule.rights_changes


def rule_for(rules: Iterable[AgentRule], role: str, connector_id: str) -> AgentRule | None:
    """The rule that counts for a personal agent of *role* calling *connector_id*.

    A rule that names the connector wins over a rule without
    ``connectors``. Between rules that match equally, the stricter one
    wins: the lower ``up_to``; with the same ``up_to``, the rule that does
    not allow changes of rights.
    """
    of_role = [rule for rule in rules if role in rule.roles]
    named = [rule for rule in of_role if connector_id in rule.connectors]
    candidates = named or [rule for rule in of_role if not rule.connectors]
    return min(candidates, key=_strictness, default=None)


def allowed_level(rule: AgentRule, role: str, limit: str) -> str:
    """The highest level *rule* lets run at once: the lowest of the rule, *limit* and the role."""
    levels = (rule.up_to, limit, ROLE_LIMITS[role])
    return min(levels, key=LIMIT_LEVELS.index)


@dataclass(frozen=True)
class RuleOp:
    """What the rule check needs to know about one operation.

    ``level`` is the level the rule compares with: the operation's risk
    level, raised by the policy, and ``dangerous`` for an operation with
    the plain "always ask" mark. ``keeps_waiting`` is true when a check
    that no rule lowers makes every call wait (a fixed rule, a per-call
    approval, "always wait", a root shell, an operation that is hard to
    undo, a multi-step operation).
    """

    connector_id: str
    op_id: str
    level: str
    changes_rights: bool
    keeps_waiting: bool


def rule_lets_run(rule: AgentRule, role: str, limit: str, op: RuleOp) -> bool:
    """True when *rule* lets a personal agent of *role* run *op* at once."""
    if op.keeps_waiting or op.level not in LIMIT_LEVELS:
        return False
    if op.changes_rights and not (rule.rights_changes and op.connector_id in rule.connectors):
        return False
    return LIMIT_LEVELS.index(op.level) <= LIMIT_LEVELS.index(allowed_level(rule, role, limit))


def runs_at_once_by_role(
    rules: Sequence[AgentRule], *, limit: str, ops: Iterable[RuleOp]
) -> dict[str, list[dict[str, str]]]:
    """For each role, the operations that start to run at once under *rules*.

    Without a rule, a personal agent runs only ``safe`` operations at once,
    so a ``safe`` operation is never in the list. Each item names the
    connector, the operation, its level and the rule (:func:`rule_ref`).
    """
    out: dict[str, list[dict[str, str]]] = {role: [] for role in RULE_ROLES}
    for op in ops:
        if op.level == RISK_LEVELS[0]:
            continue
        for role in RULE_ROLES:
            rule = rule_for(rules, role, op.connector_id)
            if rule is not None and rule_lets_run(rule, role, limit, op):
                out[role].append(
                    {
                        "connector_id": op.connector_id,
                        "op_id": op.op_id,
                        "risk": op.level,
                        "rule": rule_ref(rule),
                    }
                )
    for items in out.values():
        items.sort(key=lambda item: (item["connector_id"], item["op_id"]))
    return out


def rules_above_limit(document: Any, limit: str, *, place: str) -> list[str]:
    """One sentence for each stored rule of *document* whose ``up_to`` is above *limit*.

    A stored part MEHO cannot read gets one sentence that says no rule counts.
    """
    raw = document.get("agents") if isinstance(document, Mapping) else None
    rules, mistakes = _parse(raw)
    if mistakes:
        return [f"The stored rules at {place} are not valid, so no rule counts."]
    return [
        f"`agents[{index}]` (`{rule.name}`) allows {rule.up_to}, above the new limit "
        f"{limit}. Above the limit the rule does not count."
        for index, rule in enumerate(rules)
        if LIMIT_LEVELS.index(rule.up_to) > LIMIT_LEVELS.index(limit)
    ]


def _rules_by_name(raw: Any) -> dict[str, Any]:
    items = raw if isinstance(raw, list) else []
    return {
        (item.get("name") if isinstance(item, Mapping) else None)
        or json.dumps(["not-valid", item], sort_keys=True, default=str): item
        for item in items
    }


def agents_diff(before: Any, after: Any) -> dict[str, Any]:
    """The ``agents`` part of the import diff: added, changed and removed rules, by name.

    It is ``{}`` when neither side has a rule, so the diff of a policy
    without rules has no ``agents`` section. A stored rule that is not a
    mapping keys on its whole text: the import shows it as removed.
    """
    old = _rules_by_name(before.get("agents") if isinstance(before, Mapping) else None)
    new = _rules_by_name(after.get("agents") if isinstance(after, Mapping) else None)
    if not old and not new:
        return {}
    return {
        "agents": {
            "added": [{"entry": rule} for name, rule in new.items() if name not in old],
            "changed": [
                {"before": old[name], "after": rule}
                for name, rule in new.items()
                if name in old and old[name] != rule
            ],
            "removed": [{"entry": rule} for name, rule in old.items() if name not in new],
        }
    }
