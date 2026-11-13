# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The policy rule that MEHO writes on an audit row and an approval request.

A :class:`RuleStamp` names the rule that matched a call and the version of
the policy file. The policy gate keeps it (:func:`keep_rule`); the
dispatcher takes it right after the gate (:func:`take_rule`) and binds it
to :data:`meho_backplane.operations._audit.policy_rule_var`. The audit
writers and the approval queue read it from there. The gate also runs
for previews and checks that write no row; their rule is dropped at the
start of the next gate (:func:`forget_rule`).

Today only the policy evaluator in shadow mode (``POLICY_ENGINE_SHADOW``)
makes a stamp, so ``decided`` is ``False``: the evaluator matched the rule,
but today's checks decided the call.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

__all__ = ["RuleStamp", "forget_rule", "keep_rule", "take_rule"]


@dataclass(frozen=True)
class RuleStamp:
    """The rule that matched a call, as written on the audit row and the approval request."""

    source: str
    rule_id: str
    text: str
    policy_version: int | None = None
    decided: bool = False

    def label(self) -> str:
        """The text for ``audit_log.policy_rule``, for example ``default:person.safe (shadow)``."""
        label = f"{self.source}:{self.rule_id}"
        return label if self.decided else f"{label} (shadow)"

    def as_json(self) -> dict[str, object]:
        """The value for ``approval_request.policy_rule``."""
        return {
            "source": self.source,
            "rule_id": self.rule_id,
            "text": self.text,
            "decided": self.decided,
        }


#: The rule of the last policy gate in this context, until :func:`take_rule`.
_kept: ContextVar[RuleStamp | None] = ContextVar("policy_kept_rule", default=None)


def keep_rule(stamp: RuleStamp) -> None:
    """Keep *stamp* as the rule of the policy gate that runs now."""
    _kept.set(stamp)


def forget_rule() -> None:
    """Drop the kept rule, so a gate that keeps none leaves no old rule behind."""
    _kept.set(None)


def take_rule() -> RuleStamp | None:
    """The rule the last policy gate kept, then drop it.

    ``None`` while the ``POLICY_ENGINE_SHADOW`` switch is off, when the
    shadow failed, and when no gate ran since the last take.
    """
    stamp = _kept.get()
    _kept.set(None)
    return stamp
