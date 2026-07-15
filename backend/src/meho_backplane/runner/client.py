# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Client-initiated poll/report HTTP client for the satellite runner.

The runner never listens; it dials central. This wraps an
:class:`httpx.AsyncClient` with the two calls the tick loop makes:

* :meth:`RunnerClient.fetch_assignment` — ``GET`` the runner's current
  assignment, echoing the cached digest as ``known_version`` so central
  can answer ``304 Not Modified`` (returned as the
  :data:`ASSIGNMENT_UNCHANGED` sentinel — the loop keeps its cache).
* :meth:`RunnerClient.post_results` — ``POST`` a batch of results.

Both raise a single :class:`RunnerClientError` on any transport or
non-success status; the loop treats that uniformly as "reuse the cached
assignment / spool the batch". The central routes land in #2499; this
module is exercised against :class:`httpx.MockTransport` here.

Every request carries ``Authorization: Bearer <MEHO_RUNNER_TOKEN>`` and a
bounded timeout so a hung central cannot stall a tick indefinitely.
"""

from __future__ import annotations

from typing import Final

import httpx
import structlog

from meho_backplane.runner.wire import RunnerAssignment, RunnerResultBatch

__all__ = [
    "ASSIGNMENT_UNCHANGED",
    "RunnerClient",
    "RunnerClientError",
]

_log = structlog.get_logger(__name__)

_ASSIGNMENT_PATH: Final[str] = "/api/v1/checks/assignment"
_RESULTS_PATH: Final[str] = "/api/v1/checks/results"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0


class RunnerClientError(RuntimeError):
    """A fetch or post failed (transport error or non-success status).

    The tick loop catches this and reuses the cached assignment (fetch)
    or spools the batch (post) — a down uplink is an expected condition,
    not a crash.
    """


class _AssignmentUnchanged:
    """Singleton sentinel: central answered ``304`` — keep the cache."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "ASSIGNMENT_UNCHANGED"


#: Returned by :meth:`RunnerClient.fetch_assignment` on a ``304`` so the
#: caller keeps its currently-cached assignment without treating the
#: unchanged response as an error.
ASSIGNMENT_UNCHANGED: Final[_AssignmentUnchanged] = _AssignmentUnchanged()


class RunnerClient:
    """Async HTTP client for the runner's poll/report calls."""

    def __init__(
        self,
        *,
        central_url: str,
        runner_id: str,
        token: str,
        timeout: float = _DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._runner_id = runner_id
        self._client = httpx.AsyncClient(
            base_url=central_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout),
            transport=transport,
        )

    async def __aenter__(self) -> RunnerClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_assignment(
        self, known_version: str | None
    ) -> RunnerAssignment | _AssignmentUnchanged:
        """Fetch the runner's assignment; ``304`` yields the unchanged sentinel."""
        params = {"runner": self._runner_id}
        if known_version is not None:
            params["known_version"] = known_version
        try:
            resp = await self._client.get(_ASSIGNMENT_PATH, params=params)
        except httpx.HTTPError as exc:
            raise RunnerClientError(f"assignment fetch failed: {exc!r}") from exc
        if resp.status_code == httpx.codes.NOT_MODIFIED:
            return ASSIGNMENT_UNCHANGED
        if resp.status_code != httpx.codes.OK:
            raise RunnerClientError(f"assignment fetch returned status {resp.status_code}")
        try:
            return RunnerAssignment.model_validate(resp.json())
        except ValueError as exc:
            raise RunnerClientError(f"assignment payload invalid: {exc}") from exc

    async def post_results(self, batch: RunnerResultBatch) -> None:
        """POST a result batch; raise :class:`RunnerClientError` on failure."""
        try:
            resp = await self._client.post(_RESULTS_PATH, json=batch.model_dump(mode="json"))
        except httpx.HTTPError as exc:
            raise RunnerClientError(f"results post failed: {exc!r}") from exc
        if not resp.is_success:
            raise RunnerClientError(f"results post returned status {resp.status_code}")
