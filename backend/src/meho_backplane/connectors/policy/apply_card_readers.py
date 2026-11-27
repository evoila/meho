# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Who reads which parts of the approval card of ``policy.apply``.

The card of an install import names the tenants whose stored policy the
new file affects. Only a platform admin who is a person reads those parts
(:func:`~meho_backplane.operations.platform_admin_card.card_for_reader`).
Every other reader gets the card from :func:`card_without_tenant_parts`:
the same keys, without the tenant data inside them. The stored card stays
whole.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["TENANT_DIFF_PARTS", "card_without_tenant_parts"]

#: The parts of an install import's diff that name other tenants: one item
#: per tenant, each with its ``tenant_id``. The warnings about such an item
#: start with ``Tenant <tenant_id>:``.
TENANT_DIFF_PARTS: Final[tuple[str, ...]] = ("routing_floor", "agents_above_limit")


def card_without_tenant_parts(card: dict[str, Any]) -> dict[str, Any]:
    """The card of an install import without the data of other tenants.

    The card keeps every key. Each of the :data:`TENANT_DIFF_PARTS` that
    the diff holds is an empty list, and the warnings about the tenants
    they name are left out. A card with no tenant data, and every other
    card, comes back unchanged.
    """
    diff = card.get("diff")
    if card.get("scope") != "install" or not isinstance(diff, dict):
        return card
    if not any(diff.get(part) for part in TENANT_DIFF_PARTS):
        return card
    named = tuple(
        f"Tenant {item['tenant_id']}:"
        for part in TENANT_DIFF_PARTS
        for item in diff.get(part) or []
        if isinstance(item, dict) and "tenant_id" in item
    )
    shown = {**card, "diff": {k: [] if k in TENANT_DIFF_PARTS else v for k, v in diff.items()}}
    warnings = card.get("warnings")
    if named and isinstance(warnings, list):
        shown["warnings"] = [
            w for w in warnings if not (isinstance(w, str) and w.startswith(named))
        ]
    return shown
