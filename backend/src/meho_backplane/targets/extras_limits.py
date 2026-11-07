# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Size limit for the free-form ``extras`` map of a target.

``extras`` holds operator-written notes for connectors. MEHO accepts at most
:data:`EXTRAS_MAX_NODES` values and :data:`EXTRAS_MAX_DEPTH` levels. The walk
stops at the limit, so the check itself stays cheap on a huge input. A value
that appears twice counts twice.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["EXTRAS_MAX_DEPTH", "EXTRAS_MAX_NODES", "check_extras_size"]

EXTRAS_MAX_NODES: Final[int] = 2000
EXTRAS_MAX_DEPTH: Final[int] = 8


def check_extras_size[T](value: T) -> T:
    """Return *value*, or raise ``ValueError`` when it is too big or too deep."""
    count = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        count += 1
        if count > EXTRAS_MAX_NODES:
            raise ValueError(f"extras may hold at most {EXTRAS_MAX_NODES} values")
        if isinstance(item, dict):
            children: list[Any] = [*item.keys(), *item.values()]
        elif isinstance(item, list | tuple):
            children = list(item)
        else:
            continue
        if children and depth >= EXTRAS_MAX_DEPTH:
            raise ValueError(f"extras may be at most {EXTRAS_MAX_DEPTH} levels deep")
        stack.extend((child, depth + 1) for child in children)
    return value
