# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Clear a cache now, and once more after the session saves the change.

A policy write sets a column on a row of the request's session. The change is
saved when that session commits, at the end of the request. A cached policy
read that runs between the write and the commit still reads the old row and
puts the old value back into the cache. So a write clears the cache twice: at
once, and again when the outer transaction commits.

SQLAlchemy also fires ``after_commit`` when a savepoint is released
(``begin_nested``, as the settings import uses). The outer transaction still
runs then, so the listener waits for the commit of the outer transaction.
A rollback never fires ``after_commit``; the cache then keeps the value of
the unchanged row.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import event
from sqlalchemy.orm import Session

__all__ = ["clear_now_and_after_commit"]


def clear_now_and_after_commit(session: Session | None, clear: Callable[[], None]) -> None:
    """Call *clear* now, and once more after *session* commits its outer transaction.

    *session* is the sync session that holds the change (``object_session(row)``
    or ``AsyncSession.sync_session``). ``None`` (a row in no session) calls
    *clear* only now.
    """
    clear()
    if session is None:
        return
    done = False

    def _clear_after_commit(committed: Session) -> None:
        nonlocal done
        if done or committed.in_nested_transaction():
            return
        done = True
        clear()

    event.listen(session, "after_commit", _clear_after_commit)
