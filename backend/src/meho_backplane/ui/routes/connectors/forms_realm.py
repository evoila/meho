# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The "Managed realm" field of the console target forms.

A Keycloak target must name the realm MEHO manages
(:mod:`meho_backplane.targets.required_extras`). The create form and the
edit form both carry the field, so an older target without a realm can be
fixed in the console too. The value is checked as it is typed: it is never
trimmed. Other products ignore the field.

When the saved realm is the admin realm or MEHO's own sign-in realm, the
form does not go straight back to the list: it shows the same warning
lines REST, the CLI and the approval card show (:func:`saved_with_warnings`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from meho_backplane.targets.required_extras import KEYCLOAK_PRODUCT, keycloak_realm_problem
from meho_backplane.ui.templating import get_templates

__all__ = ["is_keycloak", "realm_extras", "realm_field_error", "saved_with_warnings"]

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


def saved_with_warnings(
    request: Request, target_name: str, warnings: Sequence[str]
) -> HTMLResponse | None:
    """The "saved, with a warning" modal; ``None`` when there is no warning.

    The target is already saved. The modal swaps into the same container
    as the form and links back to the targets list.
    """
    if not warnings:
        return None
    return get_templates().TemplateResponse(
        request,
        "connectors/_saved_warning_modal.html",
        {"target_name": target_name, "warnings": list(warnings)},
    )
