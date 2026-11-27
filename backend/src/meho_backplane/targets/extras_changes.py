# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Change single keys of a target's ``extras`` map.

``extras_changes`` is the merge form of ``extras``, like the merge form of
``secret_field_map``. A value sets or replaces that key and keeps its JSON
type. ``null`` removes the key. A key that is not named keeps its value. The
merge runs when the change runs, on the map that is stored at that moment,
so a key that was added while the change waited for approval stays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from meho_backplane.targets.extras_limits import check_extras_size

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["apply_extras_changes", "extras_change_keys", "merge_extras_changes"]


def extras_change_keys(changes: Mapping[str, Any]) -> dict[str, list[str]]:
    """The key names *changes* sets and removes, sorted. Never a value."""
    return {
        "extras_keys_set": sorted(k for k, v in changes.items() if v is not None),
        "extras_keys_removed": sorted(k for k, v in changes.items() if v is None),
    }


def apply_extras_changes(
    current: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Return *current* after *changes*, without the size check.

    A value sets or replaces its key; ``None`` removes the key (also when it
    is not stored). A stored key that is not named stays, also when its
    stored value is ``null``. The approval card uses this to show the map
    that the change will store.
    """
    merged = dict(current or {})
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged


def merge_extras_changes(
    current: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stored ``extras`` after *changes* (:func:`apply_extras_changes`).

    The result keeps the size limit of a full ``extras`` map; a map that is
    too big or too deep is a 422 and nothing changes.
    """
    merged = apply_extras_changes(current, changes)
    try:
        check_extras_size(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"kind": "extras_too_large", "message": str(exc)},
        ) from exc
    return merged
