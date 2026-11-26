# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""One MEHO process for a test that runs two of them on one database.

:func:`run_meho_process` is the target of a ``spawn`` process. It reads the
settings from the environment it got from the test (``DATABASE_URL`` names
the PostgreSQL container), serves the results route of the runner in its own
Python process, posts each body as the runner ``runner-a``, and puts its
answers, how much the ``future_time`` counter of this process rose, and how
many Dashboard mails this process sent, on *out*.
"""

from __future__ import annotations

import asyncio
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from typing import Any

import httpx
import respx
from httpx import ASGITransport

__all__ = ["ProcessReport", "run_meho_process"]

#: The answers of the posts, the rise of the ``future_time`` counter, the mails.
ProcessReport = tuple[list[int], float, int]


def run_meho_process(
    bodies: list[dict[str, Any]],
    barrier: Barrier | None,
    out: Queue[ProcessReport],
) -> None:
    """Post *bodies* one by one; with *barrier*, each post waits for the other process."""
    out.put(asyncio.run(_post_all(bodies, barrier)))


async def _post_all(bodies: list[dict[str, Any]], barrier: Barrier | None) -> ProcessReport:
    import meho_backplane.checks.investigate as inv
    from meho_backplane.db.engine import dispose_engine
    from tests._oidc_jwt_helpers import mock_discovery_and_jwks, public_jwks
    from tests.test_gateway_assignment_api import _RUNNER_A_ID, _build_app, _runner_token
    from tests.test_runner_checks_count import _KEY, _outcome

    mails: list[object] = []
    inv._schedule_investigation = lambda **_kw: None  # type: ignore[assignment]
    inv.schedule_dashboard_notification = mails.append  # type: ignore[assignment]
    count_at_start = _outcome("future_time")
    answers: list[int] = []
    headers = {"Authorization": f"Bearer {_runner_token(_KEY, runner_id=_RUNNER_A_ID)}"}
    async with httpx.AsyncClient(
        transport=ASGITransport(app=_build_app()), base_url="http://testserver"
    ) as client:
        with respx.mock(assert_all_mocked=False) as mock_router:
            mock_discovery_and_jwks(mock_router, public_jwks(_KEY))
            for body in bodies:
                if barrier is not None:
                    await asyncio.to_thread(barrier.wait, 60)
                resp = await client.post("/api/v1/checks/results", json=body, headers=headers)
                answers.append(resp.status_code)
    await dispose_engine()
    return answers, _outcome("future_time") - count_at_start, len(mails)
