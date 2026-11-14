# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Field reads and cut reports for ``k8s.cr.list`` / ``k8s.cr.info``.

The ``spec_excerpt`` and ``status_excerpt`` of a custom-resource row are
cut at a byte limit after the keys are sorted. Two helpers here make the
cut safe to work with:

* :func:`omitted_top_level_keys` names the top-level keys that are not
  whole in a cut excerpt, so a cut is never silent.
* :func:`select_fields` reads chosen paths (for example
  ``spec.schedule`` or ``status.conditions``) from the full, already
  redacted object. The answer is not cut, whatever the size of the object.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["FIELD_PATH_PATTERN", "MAX_FIELD_PATHS", "omitted_top_level_keys", "select_fields"]

#: A path starts at ``spec`` or ``status`` and goes down by dots. A number
#: picks an item of a list. Metadata is read by the other columns.
FIELD_PATH_PATTERN = r"^(spec|status)(\.[^.\s]+)*$"

#: Most paths one call may ask for.
MAX_FIELD_PATHS = 20


def omitted_top_level_keys(value: Any, max_bytes: int) -> list[str]:
    """Return the top-level keys that are not whole in the cut JSON of *value*.

    The text is ``json.dumps(value, sort_keys=True)`` cut at *max_bytes*.
    A key is whole when its ``"key": value`` part ends inside the cut.
    Returns an empty list when *value* is not a dict.
    """
    if not isinstance(value, dict):
        return []
    omitted: list[str] = []
    position = 1  # the opening brace
    for key in sorted(value, key=str):
        part = json.dumps(str(key)) + ": " + json.dumps(value[key], sort_keys=True, default=str)
        position += len(part.encode("utf-8"))
        if position > max_bytes:
            omitted.append(str(key))
        position += 2  # the ", " between two parts
    return omitted


def _read_path(obj: Any, path: str) -> tuple[bool, Any]:
    current = obj
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit() and int(segment) < len(current):
            current = current[int(segment)]
        else:
            return False, None
    return True, current


def select_fields(obj: dict[str, Any], paths: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Return ``(found, missing)`` for *paths* read from *obj*.

    *obj* must already be redacted. A path that is not in the object is
    listed in ``missing``, so a missing field never looks like a null one.
    """
    found: dict[str, Any] = {}
    missing: list[str] = []
    for path in paths:
        ok, value = (
            _read_path(obj, path) if path.split(".")[0] in ("spec", "status") else (False, None)
        )
        if ok:
            found[path] = value
        else:
            missing.append(path)
    return found, missing
