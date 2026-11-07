# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Count the changes of a cache key, so a read that overlaps a change is not cached.

A cached policy read runs in steps: read the row, await the session cleanup,
store the value. A clear of the cache during the first steps is lost when the
last step stores the value the read saw. So the loader notes the count of its
key before the read and stores the value only when the count is still the
same. Every clear bumps the count. Companion of
:func:`~meho_backplane.db.after_commit.clear_now_and_after_commit`: the clear
after the commit bumps the count, and a read that started before the commit
drops its result.
"""

from __future__ import annotations

from collections.abc import Hashable

__all__ = ["ChangeCounter"]


class ChangeCounter:
    """Per-key change count for one cache. Keys that never changed count as 0."""

    def __init__(self) -> None:
        self._counts: dict[Hashable, int] = {}

    def current(self, key: Hashable) -> int:
        """Return the count of *key*. A loader takes it before its read."""
        return self._counts.get(key, 0)

    def bump(self, key: Hashable) -> None:
        """Note a change of *key*. Every cache clear of the key calls this."""
        self._counts[key] = self._counts.get(key, 0) + 1

    def unchanged_since(self, key: Hashable, seen: int) -> bool:
        """Whether *key* had no change since the count *seen* was taken."""
        return self._counts.get(key, 0) == seen

    def reset_for_testing(self) -> None:
        """Forget every count (test isolation only)."""
        self._counts.clear()
