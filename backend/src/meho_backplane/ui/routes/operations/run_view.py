# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Console: where a Run from the operations page is now (#5111).

The Run button starts the operation as a background run
(:meth:`~meho_backplane.operations.operation_run_service.OperationRunService.submit_call`)
and answers within :data:`ANSWER_WAIT_SECONDS`. The dialog then asks
``GET /ui/operations/runs/{run_id}`` every :data:`POLL_SECONDS` seconds.
Both read the stored run, so they work on every MEHO server process.

:func:`run_view` turns a stored run into the small dict the template
``operations/_run_state.html`` draws. Only ``running`` keeps the page
asking. Every other state is final, also a run that ended without a stored
result (a crash, a server restart, a cancel), so nothing shows "running"
forever.
"""

from __future__ import annotations

from typing import Any, Final

from meho_backplane.db.models import OperationRun, OperationRunStatus
from meho_backplane.operations import operation_run as run_lifecycle

__all__ = ["ANSWER_WAIT_SECONDS", "POLL_SECONDS", "run_view"]

#: How long the Run waits for the run before it answers. A quick run shows
#: its result at once; a longer one shows "running".
ANSWER_WAIT_SECONDS: Final[float] = 1.0

#: How often the dialog asks the server again.
POLL_SECONDS: Final[int] = 3


def run_view(row: OperationRun) -> dict[str, Any]:
    """Say where the run in *row* is.

    Returns ``state`` (``running``, ``finished``, ``failed``, ``uncertain``,
    ``cancelled`` or ``no_result``), ``polling`` (ask again: the run is not
    over), ``run_id``, and the ``op_id`` and ``connector_id`` it was
    started with. ``finished`` means the run stored a result envelope, which
    can still say that the operation failed or was denied.
    """
    status = OperationRunStatus(row.status)
    if status not in run_lifecycle.TERMINAL_STATUSES:
        state = "running"
    elif status == OperationRunStatus.SUCCEEDED:
        state = "finished" if isinstance(row.result, dict) else "no_result"
    elif status == OperationRunStatus.UNCERTAIN:
        state = "uncertain"
    elif status == OperationRunStatus.CANCELLED:
        state = "cancelled"
    else:
        state = "failed"
    return {
        "state": state,
        "polling": state == "running",
        "run_id": str(row.id),
        "op_id": row.op_id,
        "connector_id": row.connector_id,
    }
