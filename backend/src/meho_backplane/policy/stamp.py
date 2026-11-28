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

The policy evaluator in shadow mode (``POLICY_ENGINE_SHADOW``) makes the
stamp, so ``decided`` is ``False``: the evaluator matched the rule, but
today's checks decided the call. With ``CUSTOM_POLICY`` on, the dispatcher
turns it into the deciding rule (:func:`deciding_rule`).
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace

from meho_backplane.db.models import PermissionVerdict
from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = ["RuleStamp", "deciding_rule", "forget_rule", "keep_rule", "row_rule", "take_rule"]

#: The gate's verdict in the words of the evaluator (``policy.evaluate.Verdict``).
_EVALUATOR_VERDICT: dict[PermissionVerdict, str] = {
    PermissionVerdict.AUTO_EXECUTE: "run",
    PermissionVerdict.NEEDS_APPROVAL: "wait",
    PermissionVerdict.DENY: "deny",
}


@dataclass(frozen=True)
class RuleStamp:
    """The rule that matched a call, as written on the audit row and the approval request.

    ``verdict`` is the evaluator's answer (``run``, ``wait`` or ``deny``)
    on a stamp the shadow kept. ``shadow`` is set only on a ``gate`` stamp:
    the evaluator's rule when its answer was another one.
    """

    source: str
    rule_id: str
    text: str
    policy_version: int | None = None
    decided: bool = False
    verdict: str | None = None
    shadow: RuleStamp | None = None

    def label(self) -> str:
        """The text for ``audit_log.policy_rule``, for example ``default:person.safe (shadow)``."""
        label = f"{self.source}:{self.rule_id}"
        return label if self.decided else f"{label} (shadow)"

    def as_json(self) -> dict[str, object]:
        """The value for ``approval_request.policy_rule``."""
        value: dict[str, object] = {
            "source": self.source,
            "rule_id": self.rule_id,
            "text": self.text,
            "decided": self.decided,
        }
        if self.shadow is not None:
            value["shadow"] = self.shadow.as_json()
        return value


def deciding_rule(kept: RuleStamp | None, verdict: str) -> RuleStamp:
    """The rule for the audit row of a call whose answer is *verdict*.

    *verdict* is ``run``, ``wait`` or ``deny``. When the evaluator's stamp
    *kept* has the same answer, it is the deciding rule. Otherwise, and
    when the shadow kept no stamp (it failed), the gate decided:
    ``gate:<verdict>``, with *kept* as its ``shadow``.
    """
    if kept is not None and kept.verdict == verdict:
        return replace(kept, decided=True)
    return RuleStamp(
        source="gate",
        rule_id=verdict,
        text=f"the policy gate answered {verdict}",
        decided=True,
        shadow=kept,
    )


def row_rule(kept: RuleStamp | None, verdict: PermissionVerdict) -> RuleStamp | None:
    """The rule for the audit row of a call that the gate answered with *verdict*.

    *kept* is the stamp the shadow kept for the call (:func:`take_rule`).
    With ``CUSTOM_POLICY`` off it stays as it is: a rule that did not
    decide, or none. With it on, :func:`deciding_rule` names the rule that
    decided.
    """
    if not custom_policy_on(get_settings()):
        return kept
    return deciding_rule(kept, _EVALUATOR_VERDICT[verdict])


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
