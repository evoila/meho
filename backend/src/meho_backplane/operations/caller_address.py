# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The floor for operations that connect to an address the caller gives.

A parameter that holds an address MEHO connects to (a URL, a host name, a
host and port), or that the target connects to for MEHO, carries the schema
keyword ``x-meho-caller-address: true``. The keyword counts at any depth of
the parameter schema, also inside nested objects and lists.

An operation with such a parameter runs at risk level ``caution`` or higher
and waits for a person's approval. The floor only raises: a connector may
set a higher level, never a lower one. MEHO applies it in three places:

* when a typed or multi-step operation is registered
  (:func:`~meho_backplane.operations.typed_register.register_typed_operation`);
* when an API description is ingested
  (:func:`~meho_backplane.operations.ingest.safety_floors.apply_safety_floor`);
* when an operation is looked up to run, after the operator overrides
  (:func:`~meho_backplane.operations.op_policy.effective_policy_map`). So an
  override that turns approval off does not remove it.
"""

from __future__ import annotations

from typing import Final

__all__ = ["CALLER_ADDRESS_MARKER", "has_caller_address", "raise_for_caller_address"]

CALLER_ADDRESS_MARKER: Final[str] = "x-meho-caller-address"
"""The schema keyword that marks a parameter holding an address MEHO connects to."""

FLOOR_SAFETY_LEVEL: Final[str] = "caution"

_SAFETY_RANK: Final[dict[str, int]] = {"safe": 0, "caution": 1, "dangerous": 2, "destructive": 3}


def has_caller_address(schema: object) -> bool:
    """Return whether *schema* marks a parameter as an address, at any depth."""
    if isinstance(schema, dict):
        if schema.get(CALLER_ADDRESS_MARKER) is True:
            return True
        return any(has_caller_address(value) for value in schema.values())
    if isinstance(schema, list):
        return any(has_caller_address(item) for item in schema)
    return False


def raise_for_caller_address(
    parameter_schema: object, *, safety_level: str, requires_approval: bool
) -> tuple[str, bool]:
    """Return the risk level and approval flag after the floor.

    For a marked *parameter_schema* the level is at least ``caution`` and
    approval is on. Otherwise both values come back unchanged.
    """
    if not has_caller_address(parameter_schema):
        return safety_level, requires_approval
    if _SAFETY_RANK.get(safety_level, 0) < _SAFETY_RANK[FLOOR_SAFETY_LEVEL]:
        safety_level = FLOOR_SAFETY_LEVEL
    return safety_level, True
