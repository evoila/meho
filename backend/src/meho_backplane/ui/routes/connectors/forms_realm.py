# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Managed realm" field of the console target forms.

A Keycloak target must name the realm MEHO manages
(:mod:`meho_backplane.targets.required_extras`). The create form and the
edit form both carry the field, so an older target without a realm can be
fixed in the console too. The value is checked as it is typed: it is never
trimmed. Other products ignore the field.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from meho_backplane.targets.required_extras import KEYCLOAK_PRODUCT, keycloak_realm_problem

__all__ = ["is_keycloak", "realm_extras", "realm_field_error"]

_MISSING = "A Keycloak target must name the realm MEHO manages."


def is_keycloak(product: str) -> bool:
    """True when the form's product is Keycloak."""
    return product.strip().lower() == KEYCLOAK_PRODUCT


def realm_field_error(product: str, managed_realm: str | None) -> dict[str, str] | None:
    """The field error for the realm, or ``None`` when the form may go on."""
    if not is_keycloak(product):
        return None
    if not managed_realm or not managed_realm.strip():
        return {"managed_realm": _MISSING}
    problem = keycloak_realm_problem({"managed_realm": managed_realm})
    return {"managed_realm": problem} if problem is not None else None


def realm_extras(current: Mapping[str, Any] | None, managed_realm: str) -> dict[str, Any]:
    """The target's extras with ``managed_realm`` set; every other key stays."""
    extras = dict(current) if isinstance(current, Mapping) else {}
    extras["managed_realm"] = managed_realm
    return extras
