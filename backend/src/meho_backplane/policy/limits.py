# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``limits`` part of the install's policy file: how far a tenant may go.

The platform admin sets it in the install's policy file only::

    limits:
      personal_agent_up_to: safe   # safe, caution or dangerous

``personal_agent_up_to`` is the highest risk level that a tenant's rule for
personal agents may let run at once. Without the part, the limit is
``safe``: that is what MEHO does today. ``destructive`` is never a limit:
a ``destructive`` operation always waits.

The part needs the ``CUSTOM_POLICY`` switch. The caller says whether the
switch is on; this module never reads a setting. With the switch off, a
file with ``limits`` is refused.

A limit only bounds what a tenant's rule may let run at once. It never
lets an operation run that a fixed rule in the code makes wait.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, field_validator

from meho_backplane.policy.classification import Scope

__all__ = [
    "DEFAULT_PERSONAL_AGENT_LIMIT",
    "LIMIT_LEVELS",
    "PolicyLimits",
    "limits_diff",
    "limits_mistakes",
    "personal_agent_limit",
]

#: The risk levels a limit can have, lowest first. ``destructive`` always waits.
LIMIT_LEVELS: Final[tuple[str, ...]] = ("safe", "caution", "dangerous")

#: The limit when the install's file has no ``limits`` part: today's answer.
DEFAULT_PERSONAL_AGENT_LIMIT: Final[str] = "safe"


class PolicyLimits(BaseModel):
    """``limits``: the outer limit a tenant's policy stays inside."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    personal_agent_up_to: str | None = None

    @field_validator("personal_agent_up_to")
    @classmethod
    def _known_level(cls, value: str | None) -> str | None:
        if value is not None and value not in LIMIT_LEVELS:
            raise ValueError(
                "is not a level a limit can have; use safe, caution or dangerous. "
                "A destructive operation always waits"
            )
        return value


def limits_mistakes(document: Mapping[str, Any], *, scope: Scope, custom_policy: bool) -> list[str]:
    """The mistakes of the ``limits`` part of *document*, a valid file's stored form."""
    if not document.get("limits"):
        return []
    if scope == "tenant":
        return [
            "`limits` belongs to the install's policy file. Only a platform admin sets "
            "the outer limit; a tenant's file stays inside it."
        ]
    if not custom_policy:
        return ["`limits` needs the CUSTOM_POLICY switch, and it is off on this MEHO."]
    return []


def limits_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The ``limits`` part of the import diff: added, changed and removed, one item per key.

    It is ``{}`` when neither side has a limit, so the diff of a policy
    without limits stays as it was.
    """
    old = _mapping(before.get("limits"))
    new = _mapping(after.get("limits"))
    if not old and not new:
        return {}
    out: dict[str, list[dict[str, Any]]] = {"added": [], "changed": [], "removed": []}
    for key in sorted({*old, *new}):
        if key not in old:
            out["added"].append({"key": key, "value": new[key]})
        elif key not in new:
            out["removed"].append({"key": key, "value": old[key]})
        elif old[key] != new[key]:
            out["changed"].append({"key": key, "before": old[key], "after": new[key]})
    return {"limits": out}


def _mapping(value: Any) -> Mapping[str, Any]:
    """*value* when it is a mapping; a stored value of another type counts as no limit."""
    return value if isinstance(value, Mapping) else {}


def personal_agent_limit(install_document: Mapping[str, Any] | None) -> str:
    """The highest level a tenant's personal agent rule may let run at once.

    *install_document* is the install's stored policy. Without a
    ``limits`` part, or with a value that is not a limit level, the answer
    is ``safe``, the lowest level.
    """
    level = _mapping((install_document or {}).get("limits")).get("personal_agent_up_to")
    return level if level in LIMIT_LEVELS else DEFAULT_PERSONAL_AGENT_LIMIT
