# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer of ``POST /api/v1/policy/explain`` (:mod:`meho_backplane.policy.explain`)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Answer",
    "ExplainApprovers",
    "ExplainAsker",
    "ExplainClassification",
    "ExplainEntry",
    "ExplainEvaluator",
    "ExplainRule",
    "ExplainStoredRequest",
    "ExplainToday",
    "PolicyExplainResponse",
]

Answer = Literal["run", "wait", "deny"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class ExplainRule(_Frozen):
    """One rule. ``path`` is its place in the policy, when the policy set it."""

    source: str = Field(description="floor, role_floor, default, agent_grant, standing_grant, ...")
    rule_id: str
    text: str
    verdict: Answer | None = Field(default=None, description="The answer of this step.")
    path: str | None = Field(
        default=None, description="Place in the policy, for example tenant:classifications[2]."
    )


class ExplainEvaluator(_Frozen):
    """The answer of the policy evaluator. It decides nothing yet (shadow mode)."""

    verdict: Answer
    rule: ExplainRule = Field(description="The rule that decided.")
    floor: ExplainRule | None = Field(
        default=None, description="The floor that decided, when a floor decided."
    )
    rules: list[ExplainRule] = Field(description="Every rule the evaluator matched, in order.")
    same_as_today: bool


class ExplainToday(_Frozen):
    """Today's answer: what the policy gate does with the call now."""

    verdict: Answer
    policy_verdict: str = Field(description="auto-execute, needs-approval or deny.")
    reason: str | None = None


class ExplainApprovers(_Frozen):
    """The login groups that may approve another person's call, and the rule that says so."""

    groups: list[str]
    rule: str | None = Field(
        default=None,
        description="default, rights_change, or the routing entries that fit, the install's "
        "first, such as install:routing[0], tenant:routing[1]. For a waiting request: "
        "category_not_stored when it started to wait before MEHO stored its category and an "
        "install entry that matches on a category could fit it; then no group may approve it. "
        "Null for a caller who is not a tenant admin or a platform admin.",
    )
    floor: str | None = Field(
        default=None,
        description="The install's routing entry that fits, such as install:routing[0]. It is "
        "a floor: a tenant's routing can only narrow it. For a waiting request it is the "
        "install's routing as it is now, as at the approve click. Null when no install entry "
        "fits, and for a caller who is not a tenant admin or a platform admin.",
    )


class ExplainEntry(_Frozen):
    """One classification entry that matches the operation."""

    path: str
    category: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    risk: str | None = None
    always_wait: bool | None = None


class ExplainClassification(_Frozen):
    """The values the call is checked with, and the classification entries that matched."""

    risk_level: str
    requires_approval: bool
    category: str | None = Field(
        default=None,
        description="Category, labels and entries are shown only to a tenant admin "
        "or a platform admin.",
    )
    labels: dict[str, str] = Field(default_factory=dict)
    entries: list[ExplainEntry] = Field(
        default_factory=list, description="Every matching entry, least specific first."
    )


class ExplainAsker(_Frozen):
    """Who the question is for."""

    me: bool
    kind: str
    role: str
    groups: list[str] = Field(default_factory=list)
    via: Literal["web", "token"]


class ExplainStoredRequest(_Frozen):
    """The rule stored on an approval request."""

    request_id: UUID
    op_id: str
    connector_id: str
    status: str
    risk_level: str | None = None
    policy_rule: dict[str, Any] | None = Field(
        default=None, description="{source, rule_id, text, decided}; null when none was stored."
    )
    policy_version: int | None = None
    approvers: ExplainApprovers


class PolicyExplainResponse(_Frozen):
    """The answer of ``POST /api/v1/policy/explain``.

    ``status`` is ``error`` when the operation or the target cannot be
    found; ``error`` and ``extras`` then say why, as in the preview.
    """

    status: Literal["ok", "error"]
    engine: Literal["off", "shadow"] = Field(
        description="off: the evaluator does not run. shadow: it runs, but today's checks decide."
    )
    connector_id: str | None = None
    op_id: str | None = None
    target: str | None = None
    file: bool = Field(
        default=False, description="True when a policy file that is not imported was asked."
    )
    asker: ExplainAsker | None = None
    answer: Answer | None = Field(
        default=None, description="What MEHO does with the call now: run, wait or deny."
    )
    decided_by: Literal["today"] | None = None
    approvers: ExplainApprovers | None = Field(
        default=None, description="Who may approve; only when the answer is wait."
    )
    today: ExplainToday | None = None
    evaluator: ExplainEvaluator | None = Field(
        default=None, description="Null while POLICY_ENGINE_SHADOW is off."
    )
    classification: ExplainClassification | None = None
    request: ExplainStoredRequest | None = None
    error: str | None = None
    extras: dict[str, Any] | None = None
