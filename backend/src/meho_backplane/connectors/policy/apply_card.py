# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How each reader sees the stored approval card of a policy import.

The stored card stays as it was planned; :func:`policy_card_as_read` gives
a copy for the reader (:func:`~meho_backplane.operations._preview.register_card_view`):

* With the ``CUSTOM_POLICY`` switch off, the card has none of the parts that
  only the switch adds: ``agents``, ``runs_at_once``, ``agents_above_limit``
  and its warnings, and ``planned_with_custom_policy``. A card planned with
  the switch off has none of them, so it reads as it is stored.
* With the switch on, a reader that is not a person (the MCP approval
  tools, and an agent, service or runner login over REST) gets, for each
  role of ``runs_at_once``, the count, the first :data:`SHORT_LIST_ITEMS`
  items, and "N more" when there are more. A person gets the whole list.

An approval card is not an operation result, so it has no result link.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = ["SHORT_LIST_ITEMS", "policy_card_as_read"]

#: How many items of each role's list a reader that is not a person gets.
SHORT_LIST_ITEMS: Final[int] = 50

#: The ``diff`` sections that only the switch adds.
_SWITCHED_SECTIONS: Final[tuple[str, ...]] = ("agents", "runs_at_once", "agents_above_limit")


def _above_limit_warnings(diff: dict[str, Any]) -> set[str]:
    """The warnings that :func:`~meho_backplane.policy.agent_rules_plan.agents_plan` adds."""
    found = diff.get("agents_above_limit")
    if not isinstance(found, list):
        return set()
    return {
        f"Tenant {item.get('tenant_id')}: {sentence}"
        for item in found
        if isinstance(item, dict)
        for sentence in item.get("entries") or []
    }


def _without_switched_parts(card: dict[str, Any]) -> dict[str, Any]:
    diff = card.get("diff")
    has_section = isinstance(diff, dict) and any(key in diff for key in _SWITCHED_SECTIONS)
    if "planned_with_custom_policy" not in card and not has_section:
        return card
    shown = {key: value for key, value in card.items() if key != "planned_with_custom_policy"}
    if isinstance(diff, dict):
        hidden = _above_limit_warnings(diff)
        shown["diff"] = {key: value for key, value in diff.items() if key not in _SWITCHED_SECTIONS}
        if isinstance(card.get("warnings"), list):
            shown["warnings"] = [line for line in card["warnings"] if line not in hidden]
    return shown


def _short(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    short: dict[str, Any] = {"count": len(items), "items": items[:SHORT_LIST_ITEMS]}
    if len(items) > SHORT_LIST_ITEMS:
        short["more"] = f"{len(items) - SHORT_LIST_ITEMS} more"
    return short


def _with_short_lists(card: dict[str, Any]) -> dict[str, Any]:
    diff = card.get("diff")
    if not isinstance(diff, dict) or not isinstance(diff.get("runs_at_once"), dict):
        return card
    lists: dict[str, Any] = diff["runs_at_once"]
    short = {role: _short(items) for role, items in lists.items()}
    return {**card, "diff": {**diff, "runs_at_once": short}}


def policy_card_as_read(effect: dict[str, Any], whole: bool) -> dict[str, Any]:
    """The stored ``proposed_effect`` of a policy import as the reader gets it.

    *whole* is true for a person, who gets each role's whole list.
    """
    card = effect.get("preview")
    if not isinstance(card, dict):
        return effect
    if not custom_policy_on(get_settings()):
        shown = _without_switched_parts(card)
    elif not whole:
        shown = _with_short_lists(card)
    else:
        return effect
    return effect if shown is card else {**effect, "preview": shown}
