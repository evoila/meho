# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How each reader sees the plan of a policy import.

The stored card and the stored revision stay as they were planned. Each
reader gets a copy: :func:`policy_card_as_read` for an approval card
(:func:`~meho_backplane.operations.platform_admin_card.register_card_view`),
:func:`diff_as_read` for the ``diff`` of an entry of the policy history,
and :func:`with_short_lists` for the card that ``preview_operation`` gives.

* With the ``CUSTOM_POLICY`` switch off, the plan has none of the parts that
  only the switch adds: ``agents``, ``runs_at_once``, ``agents_above_limit``
  and its warnings, ``planned_with_custom_policy`` and
  ``planned_with_install_limits``. A plan made with the switch off has none
  of them, so it reads as it is stored.
* With the switch on, a reader that gets the short form (every MCP answer,
  and a caller that is not a person over REST) gets, for each role of
  ``runs_at_once``, the count, the first :data:`SHORT_LIST_ITEMS` items, and
  "N more" when there are more. A person over REST and in the web console
  gets the whole list.

An approval card is not an operation result, so it has no result link.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.policy.switches import custom_policy_on
from meho_backplane.settings import get_settings

__all__ = [
    "PLANNED_WITH_INSTALL_KEY",
    "SHORT_LIST_ITEMS",
    "diff_as_read",
    "policy_card_as_read",
    "with_short_lists",
]

#: The key of a tenant import's card (``CUSTOM_POLICY`` on) that records the
#: hash of the install's limits and classifications it was planned with.
PLANNED_WITH_INSTALL_KEY: Final[str] = "planned_with_install_limits"

#: How many items of each role's list a reader of the short form gets.
SHORT_LIST_ITEMS: Final[int] = 50

#: The ``diff`` sections that only the switch adds.
_SWITCHED_SECTIONS: Final[tuple[str, ...]] = ("agents", "runs_at_once", "agents_above_limit")

#: The card keys that only the switch adds.
_SWITCHED_KEYS: Final[tuple[str, ...]] = ("planned_with_custom_policy", PLANNED_WITH_INSTALL_KEY)


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


def _short(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    short: dict[str, Any] = {"count": len(items), "items": items[:SHORT_LIST_ITEMS]}
    if len(items) > SHORT_LIST_ITEMS:
        short["more"] = f"{len(items) - SHORT_LIST_ITEMS} more"
    return short


def _diff_short(diff: dict[str, Any]) -> dict[str, Any]:
    lists = diff.get("runs_at_once")
    if not isinstance(lists, dict):
        return diff
    return {**diff, "runs_at_once": {role: _short(items) for role, items in lists.items()}}


def diff_as_read(diff: Any, *, whole: bool) -> Any:
    """The ``diff`` of a stored plan as the reader gets it; *whole* gives each role's whole list."""
    if not isinstance(diff, dict):
        return diff
    if not custom_policy_on(get_settings()):
        if not any(key in diff for key in _SWITCHED_SECTIONS):
            return diff
        return {key: value for key, value in diff.items() if key not in _SWITCHED_SECTIONS}
    return diff if whole else _diff_short(diff)


def with_short_lists(card: dict[str, Any]) -> dict[str, Any]:
    """*card* with each role's list of ``runs_at_once`` in the short form."""
    diff = card.get("diff")
    if not isinstance(diff, dict):
        return card
    short = _diff_short(diff)
    return card if short is diff else {**card, "diff": short}


def _without_switched_parts(card: dict[str, Any]) -> dict[str, Any]:
    diff = card.get("diff")
    has_section = isinstance(diff, dict) and any(key in diff for key in _SWITCHED_SECTIONS)
    if not has_section and not any(key in card for key in _SWITCHED_KEYS):
        return card
    shown = {key: value for key, value in card.items() if key not in _SWITCHED_KEYS}
    if isinstance(diff, dict):
        hidden = _above_limit_warnings(diff)
        shown["diff"] = diff_as_read(diff, whole=True)
        if isinstance(card.get("warnings"), list):
            shown["warnings"] = [line for line in card["warnings"] if line not in hidden]
    return shown


def policy_card_as_read(effect: dict[str, Any], whole: bool) -> dict[str, Any]:
    """The stored ``proposed_effect`` of a policy import as the reader gets it.

    *whole* is true for a person over REST and in the web console, who gets
    each role's whole list.
    """
    card = effect.get("preview")
    if not isinstance(card, dict):
        return effect
    if not custom_policy_on(get_settings()):
        shown = _without_switched_parts(card)
    elif not whole:
        shown = with_short_lists(card)
    else:
        return effect
    return effect if shown is card else {**effect, "preview": shown}
