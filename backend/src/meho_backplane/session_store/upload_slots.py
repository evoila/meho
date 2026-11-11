# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""How many uploads MEHO takes at the same time.

* One person has at most one upload in progress. A second one at the same
  time gets :class:`UploadBusyError`.
* At most :data:`MAX_IN_PROGRESS` uploads are in progress in all. Each
  one holds its archive in memory and a database connection while it
  waits for a worker process (:mod:`.worker`).

The uploader tries again later. A retry of the same bytes is safe.

The counts live in this process. MEHO runs one server process, and every
upload runs on its one event loop, so no lock is needed.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Final

from meho_backplane.auth.operator import Operator
from meho_backplane.session_store.worker import WORKERS

__all__ = ["MAX_IN_PROGRESS", "UploadBusyError", "upload_slot"]

#: The uploads that run, plus as many that wait for a worker.
MAX_IN_PROGRESS: Final[int] = 2 * WORKERS

_in_progress: set[tuple[uuid.UUID, str]] = set()


class UploadBusyError(Exception):
    """MEHO takes no more uploads right now. ``reason`` is the API detail."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@contextmanager
def upload_slot(operator: Operator) -> Iterator[None]:
    """Hold one upload slot for *operator*, or raise :class:`UploadBusyError`."""
    person = (operator.tenant_id, operator.sub)
    if person in _in_progress:
        raise UploadBusyError("session_upload_already_running")
    if len(_in_progress) >= MAX_IN_PROGRESS:
        raise UploadBusyError("session_uploads_busy")
    _in_progress.add(person)
    try:
        yield
    finally:
        _in_progress.discard(person)
