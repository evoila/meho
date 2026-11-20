# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Profiles with a connector list, read from ``MCP_CONNECTOR_PROFILES`` (#6099).

An admin names extra MCP tool profiles in one JSON object. Each profile
starts from one of the built-in tool lists and names the connector
families a session of the profile may use::

    {"k8s": {"tools": "investigate", "connectors": ["kubernetes"]}}

* ``tools`` -- ``investigate`` or ``operate``: the built-in tool list the
  profile starts from.
* ``connectors`` -- one or more connector families. A name counts for the
  connector with exactly that id, and for every connector whose id starts
  with the name and a dash. So ``kubernetes`` counts for every version of
  every Kubernetes connector.
* ``read_only`` -- ``true`` by default. A read-only profile also removes
  MEHO's own write tools, and MEHO refuses every operation that changes
  something.

The value is checked when MEHO starts. A mistake stops the start, so it
never gives a session more than the admin meant. The profiles count only
when the ``MCP_PROFILE_CONNECTOR_LISTS`` switch is on; see
:mod:`meho_backplane.mcp.profiles`.

This module has no MEHO imports, so :mod:`meho_backplane.settings` can use
it.
"""

from __future__ import annotations

import json
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "BUILT_IN_PROFILE_NAMES",
    "ConnectorProfile",
    "parse_connector_profiles",
]

#: The names of the built-in tool profiles. A configured profile cannot use
#: one of them.
BUILT_IN_PROFILE_NAMES: Final[frozenset[str]] = frozenset({"investigate", "operate"})

#: A profile name: lower-case letters, digits and dashes, at most 63
#: characters. It is part of the MCP URL and of the login token.
_PROFILE_NAME: Final[re.Pattern[str]] = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


class ConnectorProfile(BaseModel):
    """One configured profile: a built-in tool list and a connector list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tools: Literal["investigate", "operate"]
    connectors: tuple[str, ...] = Field(min_length=1)
    read_only: bool = True

    @field_validator("connectors")
    @classmethod
    def _clean_connectors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """Return the names in lower case; refuse a blank name."""
        cleaned = tuple(name.strip().lower() for name in value)
        if any(not name for name in cleaned):
            raise ValueError("a connector name must not be blank")
        return cleaned


def parse_connector_profiles(raw: object) -> dict[str, ConnectorProfile]:
    """Return the profiles of ``MCP_CONNECTOR_PROFILES``.

    *raw* is the JSON text of the setting, or an already parsed mapping.
    Empty text means no profiles. Raises :class:`ValueError` on bad JSON, a
    bad profile name, a built-in name or a bad profile.
    """
    if isinstance(raw, str):
        if not raw.strip():
            return {}
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"MCP_CONNECTOR_PROFILES is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("MCP_CONNECTOR_PROFILES must be a JSON object of profiles")
    profiles: dict[str, ConnectorProfile] = {}
    for name, spec in raw.items():
        if isinstance(spec, ConnectorProfile):
            profiles[_checked_name(name)] = spec
            continue
        profiles[_checked_name(name)] = ConnectorProfile.model_validate(spec)
    return profiles


def _checked_name(name: object) -> str:
    """Return *name* when it is a usable profile name, else raise."""
    if not isinstance(name, str) or not _PROFILE_NAME.match(name):
        raise ValueError(
            f"MCP_CONNECTOR_PROFILES: profile name {str(name)[:64]!r} must be lower-case "
            "letters, digits and dashes (at most 63 characters)"
        )
    if name in BUILT_IN_PROFILE_NAMES:
        raise ValueError(f"MCP_CONNECTOR_PROFILES: {name!r} is a built-in profile name")
    return name
