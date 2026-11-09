# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Size limit for the free-form ``extras`` map of a target.

``extras`` holds operator-written notes for connectors. MEHO accepts at most
:data:`EXTRAS_MAX_NODES` values and :data:`EXTRAS_MAX_DEPTH` levels. The walk
checks the length of each list or map against the values still allowed before
it reads the children, so the check itself stays cheap on a huge input. A value
that appears twice counts twice.
"""

from __future__ import annotations

from typing import Any, Final

__all__ = ["EXTRAS_MAX_DEPTH", "EXTRAS_MAX_NODES", "check_extras_size"]

EXTRAS_MAX_NODES: Final[int] = 2000
EXTRAS_MAX_DEPTH: Final[int] = 8


def check_extras_size[T](value: T) -> T:
    """Return *value*, or raise ``ValueError`` when it is too big or too deep."""
    # Values found so far, also those still waiting on the stack.
    seen = 1
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            size = 2 * len(item)
        elif isinstance(item, list | tuple):
            size = len(item)
        else:
            continue
        if size and depth >= EXTRAS_MAX_DEPTH:
            raise ValueError(f"extras may be at most {EXTRAS_MAX_DEPTH} levels deep")
        seen += size
        if seen > EXTRAS_MAX_NODES:
            raise ValueError(f"extras may hold at most {EXTRAS_MAX_NODES} values")
        children = [*item.keys(), *item.values()] if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children)
    return value
