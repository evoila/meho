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

__all__ = ["merge_extras_changes"]


def merge_extras_changes(
    current: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the stored ``extras`` after *changes*.

    A value sets or replaces its key; ``None`` removes the key (also when it
    is not stored). The result keeps the size limit of a full ``extras``
    map; a map that is too big or too deep is a 422 and nothing changes.
    """
    merged = dict(current or {})
    for key, value in changes.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    try:
        check_extras_size(merged)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"kind": "extras_too_large", "message": str(exc)},
        ) from exc
    return merged
