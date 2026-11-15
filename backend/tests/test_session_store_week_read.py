# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read one week of sessions: the time window of the list, and every upload.

``GET /api/v1/sessions?since=...&until=...`` lists a session when its last
upload is at or after ``since`` and it started before ``until``.
``meho sessions get`` downloads each upload of a session once. The
fixtures and helpers come from ``test_session_store_api.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.session_store.models import AgentSessionRecord

from .test_session_store_api import (  # noqa: F401 -- pytest fixtures
    _LINE_1,
    _LINE_2,
    _LINE_3,
    _SID,
    _audit,
    _env,
    _headers,
    _transcript,
    _unpack,
    _upload,
    client,
    store,
)

_SINCE = "2026-11-02T00:00:00Z"
_UNTIL = "2026-11-09T00:00:00Z"


def _at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 11, day, hour, tzinfo=UTC)


async def _set_times(
    session_id: str, *, started: datetime | None, created: datetime, last: datetime
) -> None:
    async with get_sessionmaker()() as session:
        await session.execute(
            update(AgentSessionRecord)
            .where(AgentSessionRecord.session_id == session_id)
            .values(started_at=started, created_at=created, last_seen_at=last)
        )
        await session.commit()


def _ids(client: TestClient, query: str) -> set[str]:  # noqa: F811
    resp = client.get(f"/api/v1/sessions?{query}", headers=_headers())
    assert resp.status_code == 200, resp.text
    return {item["session_id"] for item in resp.json()["items"]}


@pytest.mark.asyncio
async def test_only_the_sessions_of_the_week_are_listed(client: TestClient) -> None:  # noqa: F811
    times = {
        "before": (_at(1, 8), _at(1, 9)),
        "inside": (_at(4), _at(5)),
        "after": (_at(9, 1), _at(9, 2)),
        # Started the week before, last upload inside the week.
        "spanning": (_at(1, 22), _at(2, 3)),
    }
    for session_id, (started, last) in times.items():
        assert _upload(client, _transcript(_LINE_1), session_id=session_id).status_code == 200
        await _set_times(session_id, started=started, created=started, last=last)

    assert _ids(client, f"since={_SINCE}&until={_UNTIL}") == {"inside", "spanning"}
    assert _ids(client, f"since={_SINCE}") == {"inside", "spanning", "after"}
    assert _ids(client, f"until={_UNTIL}") == {"before", "inside", "spanning"}
    [*_, row] = await _audit("sessions.list")
    assert row.payload["until"] == "2026-11-09T00:00:00+00:00"


@pytest.mark.asyncio
async def test_a_session_without_a_start_counts_from_its_first_upload(
    client: TestClient,  # noqa: F811
) -> None:
    assert _upload(client, _transcript(_LINE_1), session_id="late").status_code == 200
    await _set_times("late", started=None, created=_at(9, 1), last=_at(9, 2))
    assert _upload(client, _transcript(_LINE_1), session_id="early").status_code == 200
    await _set_times("early", started=None, created=_at(3), last=_at(3))
    assert _ids(client, f"since={_SINCE}&until={_UNTIL}") == {"early"}


@pytest.mark.parametrize(
    ("query", "detail"),
    [
        ("since=2026-11-02T00:00:00", "since_needs_time_zone"),
        ("until=2026-11-09T00:00:00", "until_needs_time_zone"),
        (f"since={_UNTIL}&until={_SINCE}", "since_after_until"),
    ],
)
def test_a_bad_window_is_refused(client: TestClient, query: str, detail: str) -> None:  # noqa: F811
    resp = client.get(f"/api/v1/sessions?{query}", headers=_headers())
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail


@pytest.mark.asyncio
async def test_each_upload_download_writes_one_audit_row(client: TestClient) -> None:  # noqa: F811
    lines = (_LINE_1, _LINE_2, _LINE_3)
    for n in range(1, 4):
        assert _upload(client, _transcript(*lines[:n])).status_code == 200
    tails = []
    for seq in (1, 2, 3):
        resp = client.get(f"/api/v1/sessions/{_SID}/uploads/{seq}", headers=_headers())
        assert resp.status_code == 200
        tails.append(_unpack(resp.content)[f"{_SID}.jsonl"])
    assert tails == list(lines)
    rows = await _audit("sessions.download")
    assert [row.payload["seq"] for row in rows] == [1, 2, 3]
    assert all(row.status_code == 200 for row in rows)
