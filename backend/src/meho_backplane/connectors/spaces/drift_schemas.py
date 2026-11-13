# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Parameter and answer schemas, and the guidance text, of ``spaces.drift``."""

from __future__ import annotations

from typing import Any, Final

__all__ = ["DRIFT_GUIDANCE", "DRIFT_PARAMETERS", "DRIFT_RESPONSE"]

DRIFT_PARAMETERS: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
    "examples": [{}],
}


def _list(description: str) -> dict[str, Any]:
    return {"type": "array", "description": description, "items": {"type": "object"}}


DRIFT_RESPONSE: Final[dict[str, Any]] = {
    "type": "object",
    "properties": {
        "unassigned_targets": _list("Live targets that no space owns."),
        "missing_targets": _list("Current target assignments whose target is gone."),
        "unreachable_targets": _list("Assigned targets whose last probe said not reachable."),
        "missing_objects": _list("Current machine assignments seen in no live topology node."),
        "state_mismatch": _list(
            "Current assignments that disagree with the live record: the space is "
            "disabled, or the target was renamed."
        ),
        "counts": {
            "type": "object",
            "description": "The number of entries for each kind of drift.",
            "additionalProperties": {"type": "integer"},
        },
        "total_count": {"type": "integer", "description": "All counts added up."},
    },
    "required": [
        "unassigned_targets",
        "missing_targets",
        "unreachable_targets",
        "missing_objects",
        "state_mismatch",
        "counts",
        "total_count",
    ],
}

DRIFT_GUIDANCE: Final[dict[str, Any]] = {
    "when_to_call": (
        "Find where the ownership record and the live targets disagree. A nightly "
        "check runs it; call it by hand to see the names behind a count."
    ),
    "preconditions": (
        "The setting SPACES_ENABLED must be on. It reads stored data and repairs nothing."
    ),
    "output_shape": (
        "{'unassigned_targets': [...], 'missing_targets': [...], 'unreachable_targets': [...], "
        "'missing_objects': [...], 'state_mismatch': [...], 'counts': {...}, 'total_count'}"
    ),
    "next_step": (
        "Use `spaces.assign` for an unassigned target, `spaces.unassign` or "
        "`spaces.reassign` for a missing one. A person decides each repair."
    ),
    "result_scalars": {"keys": ["total_count"]},
}
