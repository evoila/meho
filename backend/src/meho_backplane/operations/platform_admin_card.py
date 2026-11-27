# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The parts of an approval card that only a platform admin reads.

Some cards hold data of other tenants. The card of an import of the
install's policy file is one: it names each tenant whose stored policy
the new file affects. MEHO stores the whole card. Each view that shows a
stored card (the REST approvals routes, the MCP approval tools, the web
console) passes it through :func:`card_for_reader`:

* a platform admin who is a person, in a call of their own, reads the
  card as stored;
* every other reader reads it as the operation's filter gives it back
  (:func:`register_platform_admin_card_filter`).

The filter gets the card (the ``preview`` part of the stored
``proposed_effect``) and gives back a new dict. It never changes the
stored one, so the run after an approval reads the card as stored. An
operation without a filter is shown as stored to every reader.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.auth.operator import is_human_principal

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator

__all__ = [
    "card_for_reader",
    "reads_platform_admin_parts",
    "register_platform_admin_card_filter",
]

#: ``op_id`` -> the filter that takes the platform admin parts out of a card.
_FILTERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}


def register_platform_admin_card_filter(
    op_id: str, card_filter: Callable[[dict[str, Any]], dict[str, Any]]
) -> None:
    """Readers who are not a platform admin read the card of *op_id* as *card_filter* gives it."""
    _FILTERS[op_id] = card_filter


def reads_platform_admin_parts(reader: Operator | None) -> bool:
    """True when *reader* is a platform admin who is a person, in a call of their own.

    ``None`` (a view that does not name its reader) is not. A call that runs
    with an acting agent (:func:`~meho_backplane.auth.delegation.actor_delegation`,
    for example a hosted agent run a platform admin started) is not either.
    """
    return (
        reader is not None
        and reader.platform_admin
        and is_human_principal(reader)
        and resolve_actor_sub() is None
    )


def card_for_reader(
    op_id: str, effect: dict[str, Any], *, reader: Operator | None
) -> dict[str, Any]:
    """The stored ``proposed_effect`` *effect* of *op_id*, as *reader* may read it."""
    card_filter = _FILTERS.get(op_id)
    if card_filter is None or reads_platform_admin_parts(reader):
        return effect
    preview = effect.get("preview")
    if not isinstance(preview, dict):
        return effect
    return {**effect, "preview": card_filter(preview)}
