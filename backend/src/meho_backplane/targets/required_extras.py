# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Settings that a target of one product must carry in its ``extras``.

A Keycloak target must name the realm it manages (``extras["managed_realm"]``).
MEHO has no built-in realm name. The Keycloak connector refuses to run on a
target without one, and a create or update of such a target is refused with
the same text.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "KEYCLOAK_PRODUCT",
    "check_keycloak_managed_realm",
    "has_managed_realm",
    "managed_realm_missing_message",
]

KEYCLOAK_PRODUCT: Final = "keycloak"


def has_managed_realm(extras: Any) -> bool:
    """True when *extras* holds a non-empty string ``managed_realm``."""
    if not isinstance(extras, Mapping):
        return False
    value = extras.get("managed_realm")
    return isinstance(value, str) and bool(value.strip())


def managed_realm_missing_message(name: str | None = None) -> str:
    """The text for a Keycloak target that names no managed realm."""
    label = f"Keycloak target '{name}'" if name else "A Keycloak target"
    return (
        f"{label} names no managed realm. Set the realm MEHO manages on the target: "
        "pass `--extra managed_realm=<realm>` to `meho targets add`, or to "
        "`meho targets update <name>`. An update replaces the whole extras map, "
        "so repeat every extras key you want to keep (for example "
        "`--extra admin_realm=master --extra managed_realm=<realm>`)."
    )


def check_keycloak_managed_realm(product: str | None, extras: Any, name: str | None = None) -> None:
    """Raise :class:`ValueError` when a Keycloak target names no managed realm."""
    if not isinstance(product, str) or product.strip().lower() != KEYCLOAK_PRODUCT:
        return
    if not has_managed_realm(extras):
        raise ValueError(managed_realm_missing_message(name))
