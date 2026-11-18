# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Write the place of a wrong input as a short path (#4174).

A path names a nested input the way a model wrote it: property names joined
with ``.``, and a list position in brackets. For example
``query.filter[0].field``.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["field_path"]


def field_path(parts: Iterable[str | int]) -> str:
    """Return *parts* as one path: ``("query", "filter", 0, "op")`` → ``query.filter[0].op``."""
    path = ""
    for part in parts:
        if isinstance(part, int):
            path += f"[{part}]"
        else:
            path += f".{part}" if path else str(part)
    return path
