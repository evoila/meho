# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The named sign-in levels of a Keycloak realm (#6281).

Keycloak names sign-in levels in the realm attribute ``acr.loa.map``, a
JSON object of name -> level. A sign-in that reached a level carries the
name in its ``acr`` claim. MEHO uses level 2, named ``meho-approve``: only
the approval sign-in flow gives it. A client can have its own
``acr.loa.map``; Keycloak then uses the client's map instead of the realm's.

The realm stage, the ``keycloak.authflow.ensure_approval_flow`` operation and
the health check of the approval sign-in use these helpers, so all three
read the map the same way. The Go bootstrap has the same rules in
``cli/internal/cmd/admin/keycloak/flows.go``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

__all__ = [
    "APPROVAL_LEVEL",
    "APPROVAL_LEVEL_NAME",
    "LEVEL_MAP",
    "client_level_map",
    "level_conflict",
    "parse_level_map",
]

#: The realm (and client) attribute that names sign-in levels.
LEVEL_MAP = "acr.loa.map"

#: The name and the number of the approval level.
APPROVAL_LEVEL_NAME = "meho-approve"
APPROVAL_LEVEL = 2


def parse_level_map(raw: Any) -> dict[str, Any] | None:
    """The level map in *raw* (the attribute value); ``None`` when it is not a JSON object.

    A missing or empty value is an empty map.
    """
    if not raw:
        return {}
    try:
        levels = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return levels if isinstance(levels, dict) else None


def level_conflict(have: Mapping[str, Any], wanted: Mapping[str, Any]) -> str | None:
    """Name the first name in *have* that holds a level of *wanted* under another name.

    For example *have* ``{"gold": 2}`` and *wanted* ``{"meho-approve": 2}``.
    Two names on one level make the ``acr`` claim not match MEHO's levels.
    ``None`` when there is no such name.
    """
    for name, level in have.items():
        if name in wanted:
            continue
        for wanted_name, wanted_level in wanted.items():
            if str(level) == str(wanted_level):
                return (
                    f"the name {name!r} already has level {level} in {LEVEL_MAP}; "
                    f"MEHO needs that level for {wanted_name!r}"
                )
    return None


def client_level_map(client: Mapping[str, Any]) -> str | None:
    """Say so when the client has its own, non-empty ``acr.loa.map``; else ``None``.

    Keycloak then uses the client's map, not the realm's, so the realm's
    approval level does not reach the client's tokens.
    """
    attributes = client.get("attributes")
    raw = attributes.get(LEVEL_MAP) if isinstance(attributes, Mapping) else None
    if parse_level_map(raw) == {}:
        return None
    return f"the client {client.get('clientId')} has its own {LEVEL_MAP}"
