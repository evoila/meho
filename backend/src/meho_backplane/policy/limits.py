# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The ``limits`` part of the install's policy file: how far a tenant may go.

The platform admin sets it in the install's policy file only::

    limits:
      personal_agent_up_to: safe   # safe, caution or dangerous

``personal_agent_up_to`` is the highest risk level that a tenant's rule for
personal agents may let run at once. Without the part, the limit is
``safe``. ``destructive`` is never a limit: a ``destructive`` operation
always waits.

The caller says whether the ``CUSTOM_POLICY`` switch is on
(``custom_policy``, default off); this module never reads a setting.

* With the switch on, the install's file may hold ``limits``. The diff, the
  export and :func:`personal_agent_limit` show and use the stored part.
* With the switch off, a file that holds the key ``limits`` is refused,
  whatever its value. A stored ``limits`` part is ignored: the export
  writes no ``limits`` part, the diff has no ``limits`` section, an
  approved import keeps the stored part as it is, and
  :func:`personal_agent_limit` answers ``safe``.
* A tenant's file that holds the key ``limits`` is refused, with the switch
  on or off.

No call decision reads the part in this version of MEHO.
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
    "not_read_mistake",
    "personal_agent_limit",
]

#: The risk levels a limit can have, lowest first. ``destructive`` always waits.
LIMIT_LEVELS: Final[tuple[str, ...]] = ("safe", "caution", "dangerous")

#: The limit when the install's file has no ``limits`` part.
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


def not_read_mistake(key: str) -> str:
    """The refusal of the reserved top-level *key* in a policy file."""
    return f"This version of MEHO does not read `{key}` in a policy file; remove it."


def limits_mistakes(document: Mapping[str, Any], *, scope: Scope, custom_policy: bool) -> list[str]:
    """The mistakes of the ``limits`` part of *document*, a valid file's stored form.

    The check reads whether the file has the key ``limits``, not whether
    the part has a value: ``limits: {}`` and ``limits: null`` count.
    """
    if "limits" not in document:
        return []
    if scope == "tenant":
        return [
            "`limits` belongs to the install's policy file. Only a platform admin sets "
            "the outer limit; a tenant's file stays inside it."
        ]
    if not custom_policy:
        return [not_read_mistake("limits")]
    return []


def limits_diff(before: Any, after: Any, *, custom_policy: bool = False) -> dict[str, Any]:
    """The ``limits`` part of the import diff: added, changed and removed, one item per key.

    It is ``{}`` with the switch off, and when neither side has a limit, so
    the diff of a policy without limits has no ``limits`` section. A side
    that is not a mapping, or a stored value that is not a mapping, counts
    as no limit.
    """
    if not custom_policy:
        return {}
    old = _mapping(_mapping(before).get("limits"))
    new = _mapping(_mapping(after).get("limits"))
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


def personal_agent_limit(install_document: Any, *, custom_policy: bool = False) -> str:
    """The highest level a tenant's personal agent rule may let run at once.

    *install_document* is the install's stored policy. With the switch off,
    the answer is ``safe``. Without a ``limits`` part, or with a stored
    value that is not a mapping or not a limit level, the answer is
    ``safe``, the lowest level.
    """
    if not custom_policy:
        return DEFAULT_PERSONAL_AGENT_LIMIT
    level = _mapping(_mapping(install_document).get("limits")).get("personal_agent_up_to")
    return level if level in LIMIT_LEVELS else DEFAULT_PERSONAL_AGENT_LIMIT
