# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``GET /ui/agents/runs/{handle}/events`` -- follow a run's saved events live.

A hosted run saves each event while it runs
(:mod:`meho_backplane.session_store.hosted_runs`). This stream reads them
from the store: first the events saved so far, then each new one as it is
saved, until the run ends. The run console uses it, so a page reload, or a
visit days later, shows the whole run.

Each frame has an SSE ``id:`` with the event's ``seq``. When the browser's
``EventSource`` connects again, it sends the last id in the
``Last-Event-ID`` header, and the stream goes on after that event. The
events come in ``seq`` order from one writer, so the stream has no gaps and
no doubles.

The last frame is ``event: end``. It carries the run's end status and the
session state: ``ended`` when the run saved its final (or error) event,
``unfinished`` when it stopped without one (a crash, a restart, a lost
lease or a cancel). MEHO never adds an ending that the run did not make.

Opening the stream writes one audit row (``sessions.run_events.read``).
The reads that follow while the stream is open write none.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Final

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from meho_backplane.agent.invocation import AgentRunNotFoundError
from meho_backplane.agent.run_events import RunEventsView, StoredRunEvent, read_run_events
from meho_backplane.auth.operator import Operator
from meho_backplane.ui.auth.middleware import UISessionContext, require_ui_session
from meho_backplane.ui.routes.agents.runs.operator import resolve_run_reader

__all__ = [
    "build_run_events_router",
    "event_frame",
    "events_response",
    "first_read",
    "tail_frames",
]

#: Seconds between two reads of the store while the run goes on.
POLL_SECONDS: float = 0.5

#: Most events one read returns. A full page is followed at once by the next.
PAGE_SIZE: Final[int] = 200

_require_session_dep = Depends(require_ui_session)
_run_reader_dep = Depends(resolve_run_reader)


def event_frame(run_id: uuid.UUID, event: StoredRunEvent) -> str:
    """Format one saved event as an SSE frame with its ``seq`` as the id."""
    payload: dict[str, object] = {"run_id": str(run_id), "seq": event.seq, **event.data}
    if event.withheld:
        payload["withheld"] = True
    elif event.quarantined and not event.data:
        payload["quarantined"] = True
    data = json.dumps(payload, separators=(",", ":"), default=str)
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {data}\n\n"


def _end_frame(view: RunEventsView, last_seq: int) -> str:
    state = view.state
    if state == "open":
        # The run ended, but its session row has not caught up yet.
        state = "unfinished"
    payload = {
        "run_id": str(view.run_id),
        "status": view.run_status,
        "state": state,
        "last_seq": last_seq,
    }
    return f"event: end\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def first_read(
    operator: Operator, run_id: uuid.UUID, *, after: int, path: str
) -> RunEventsView:
    """Read the first page (and write the audit row); 404 for an unknown run."""
    try:
        return await read_run_events(
            operator, run_id, after=after, limit=PAGE_SIZE, audit_path=path
        )
    except AgentRunNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="agent_run_not_found"
        ) from exc


async def tail_frames(
    operator: Operator, first: RunEventsView, *, after: int = 0
) -> AsyncIterator[str]:
    """Yield the saved events from *first* on, then the new ones, then ``end``."""
    view = first
    cursor = after
    while True:
        for event in view.events:
            yield event_frame(view.run_id, event)
            cursor = event.seq
        full_page = len(view.events) >= PAGE_SIZE
        if view.run_done and not full_page:
            yield _end_frame(view, cursor)
            return
        if not full_page:
            await asyncio.sleep(POLL_SECONDS)
        view = await read_run_events(operator, view.run_id, after=cursor, limit=PAGE_SIZE)


def events_response(frames: AsyncIterator[str]) -> StreamingResponse:
    """Wrap SSE *frames* in a response that no proxy buffers."""
    return StreamingResponse(
        frames,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "X-Accel-Buffering": "no",
        },
    )


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id", "").strip()
    return int(raw) if raw.isdigit() else 0


def build_run_events_router() -> APIRouter:
    """Construct the router of the run events stream."""
    router = APIRouter(tags=["ui-agents"])

    @router.get("/ui/agents/runs/{handle}/events")
    async def run_events_stream(
        request: Request,
        handle: uuid.UUID,
        session_ctx: UISessionContext = _require_session_dep,
        operator: Operator = _run_reader_dep,
    ) -> StreamingResponse:
        """Stream the run's saved events, then its new ones, as Server-Sent Events."""
        del session_ctx  # the cookie session gates the route; the operator scopes it.
        after = _last_event_id(request)
        first = await first_read(operator, handle, after=after, path=request.url.path)
        return events_response(tail_frames(operator, first, after=after))

    return router
