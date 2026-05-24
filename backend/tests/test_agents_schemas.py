# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for :mod:`meho_backplane.agents.schemas`.

Coverage (Task #809 / G11.1-T2):

* ``extra="forbid"`` -- an unknown field on create / update raises.
* Name pattern -- a path-breaking name (slash / colon / space) is
  rejected on the field and via :func:`validate_name`.
* Model tier enum -- only ``standard`` / ``fast`` / ``deep`` validate;
  the enum renders as its bare string value.
* Turn-budget bounds -- ``< 1`` and ``> 1000`` are rejected.
* Update is partial -- ``model_dump(exclude_unset=True)`` carries only
  the fields the caller set.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from meho_backplane.agents.schemas import (
    AgentDefinitionCreate,
    AgentDefinitionUpdate,
    AgentModelTier,
    validate_name,
)


def test_create_minimal_valid() -> None:
    """A minimal valid create body fills the documented defaults."""
    body = AgentDefinitionCreate(
        name="triage",
        identity_ref="agent:triage",
        model_tier=AgentModelTier.STANDARD,
        system_prompt="hi",
        turn_budget=10,
    )
    assert body.toolset == {}
    assert body.output_schema is None
    assert body.enabled is True
    assert body.model_tier is AgentModelTier.STANDARD


def test_create_rejects_unknown_field() -> None:
    """``extra="forbid"`` trips a 422-equivalent on an unknown field."""
    with pytest.raises(ValidationError):
        AgentDefinitionCreate(
            name="triage",
            identity_ref="agent:triage",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="hi",
            turn_budget=10,
            systemPrompt="typo",  # type: ignore[call-arg]
        )


@pytest.mark.parametrize("bad_name", ["has/slash", "has:colon", "has space", "", "tab\tname"])
def test_create_rejects_bad_name(bad_name: str) -> None:
    """A name outside the safe-URL alphabet is rejected at the field."""
    with pytest.raises(ValidationError):
        AgentDefinitionCreate(
            name=bad_name,
            identity_ref="agent:x",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="hi",
            turn_budget=10,
        )


@pytest.mark.parametrize("good_name", ["incident-triage", "vm.inventory-bot", "agent_1", "A1"])
def test_validate_name_accepts_safe_alphabet(good_name: str) -> None:
    """:func:`validate_name` returns the input unchanged for a safe name."""
    assert validate_name(good_name) == good_name


@pytest.mark.parametrize("bad_name", ["a/b", "a:b", "a b", "a%b"])
def test_validate_name_rejects_unsafe(bad_name: str) -> None:
    """:func:`validate_name` raises for a path-breaking name."""
    with pytest.raises(ValueError, match="outside the safe set"):
        validate_name(bad_name)


def test_create_rejects_invalid_model_tier() -> None:
    """A model tier outside the enum is rejected."""
    with pytest.raises(ValidationError):
        AgentDefinitionCreate.model_validate(
            {
                "name": "triage",
                "identity_ref": "agent:x",
                "model_tier": "ultra",
                "system_prompt": "hi",
                "turn_budget": 10,
            }
        )


@pytest.mark.parametrize("bad_budget", [0, -1, 1001, 5000])
def test_create_rejects_out_of_range_turn_budget(bad_budget: int) -> None:
    """Turn budget below 1 or above the 1000 cap is rejected."""
    with pytest.raises(ValidationError):
        AgentDefinitionCreate(
            name="triage",
            identity_ref="agent:x",
            model_tier=AgentModelTier.STANDARD,
            system_prompt="hi",
            turn_budget=bad_budget,
        )


def test_model_tier_renders_as_bare_value() -> None:
    """``AgentModelTier`` is a ``StrEnum`` -- f-string renders the value."""
    assert f"{AgentModelTier.FAST}" == "fast"
    assert AgentModelTier.DEEP.value == "deep"


def test_update_rejects_unknown_field() -> None:
    """``extra="forbid"`` on update trips on an unknown field."""
    with pytest.raises(ValidationError):
        AgentDefinitionUpdate.model_validate({"name": "renamed"})


def test_update_is_partial_via_exclude_unset() -> None:
    """An update with one field set dumps only that field under exclude_unset."""
    body = AgentDefinitionUpdate(enabled=False)
    changes = body.model_dump(exclude_unset=True)
    assert changes == {"enabled": False}


def test_update_all_fields_optional() -> None:
    """An empty update validates (every field optional)."""
    body = AgentDefinitionUpdate()
    assert body.model_dump(exclude_unset=True) == {}
