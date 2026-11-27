# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The answer of ``POST /api/v1/policy/explain`` (:mod:`meho_backplane.policy.explain`)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

__all__ = [
    "Answer",
    "ExplainApprovers",
    "ExplainAsker",
    "ExplainClassification",
    "ExplainEntry",
    "ExplainEvaluator",
    "ExplainOwnApproval",
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
        "first, such as install:routing[0], tenant:routing[1]. category_not_stored when MEHO "
        "does not know the category the install's policy gives the change (it gives it two "
        "categories, or a waiting request started before MEHO stored it) and an install entry "
        "that matches on a category could fit it; then no group may approve it. "
        "Null for a caller who is not a tenant admin or a platform admin.",
    )
    floor: str | None = Field(
        default=None,
        description="The install's routing entry that fits, such as install:routing[0]. It is "
        "a floor: a tenant's routing can only narrow it. For a waiting request it is the "
        "install's routing as it is now, as at the approve click. Null when no install entry "
        "fits, and for a caller who is not a tenant admin or a platform admin.",
    )


class ExplainOwnApproval(_Frozen):
    """May the person who asked approve the request that the call makes?

    The answer of the approve step for that request, once for each way to
    approve: a click in the web console (``web``), and the command line or
    REST (``token``). Each reason is the text the approve step shows.

    ``web`` and ``token`` are ``null`` when the approve step reads facts
    that only the call has (its values, or what MEHO reads from them or
    from the target). Then both reasons say so.
    """

    web: bool | None = Field(
        description="True when a click in the web console approves it. Null when it depends "
        "on the values of the call."
    )
    web_reason: str = Field(description="Why, in the words of the approve step.")
    token: bool | None = Field(
        description="True when an approve on the command line or REST does. Null when it "
        "depends on the values of the call."
    )
    token_reason: str = Field(description="Why, in the words of the approve step.")
    _web_reason_without_place: str | None = PrivateAttr(default=None)
    _token_reason_without_place: str | None = PrivateAttr(default=None)

    @classmethod
    def of(
        cls,
        *,
        web: bool | None,
        web_reason: tuple[str, str],
        token: bool | None,
        token_reason: tuple[str, str],
    ) -> ExplainOwnApproval:
        """The part; each reason is (text, the same text without the places of policy entries)."""
        part = cls(web=web, web_reason=web_reason[0], token=token, token_reason=token_reason[0])
        part._web_reason_without_place = web_reason[1]
        part._token_reason_without_place = token_reason[1]
        return part

    def without_place(self) -> ExplainOwnApproval:
        """The part for a caller who may not see the places of policy entries."""
        return self.model_copy(
            update={
                "web_reason": self._web_reason_without_place or self.web_reason,
                "token_reason": self._token_reason_without_place or self.token_reason,
            }
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
    own_approval: ExplainOwnApproval | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        description="Only with CUSTOM_POLICY on, and only when the answer is wait: whether "
        "the person who asked may approve their own request. Left out otherwise.",
    )
    today: ExplainToday | None = None
    evaluator: ExplainEvaluator | None = Field(
        default=None, description="Null while CUSTOM_POLICY and POLICY_ENGINE_SHADOW are off."
    )
    classification: ExplainClassification | None = None
    request: ExplainStoredRequest | None = None
    not_checked: list[str] | None = Field(
        default=None,
        description="Each check after the policy gate that explain does not ask "
        "and that can still refuse the call, by a fixed name. Empty for deny. "
        "Null for a stored approval request and for an error.",
    )
    error: str | None = None
    extras: dict[str, Any] | None = None
