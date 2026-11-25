# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A Google Cloud change reaches the target at most twice, also after a token refresh.

Every request counts toward the limit: the first send, a followed redirect
and the send after the token refresh on a 401. Google is never called: every
answer comes from ``respx``. The connector and target come from
``test_connectors_gcloud_workload_identity``.
"""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors.gcloud import GcloudConnector
from meho_backplane.connectors.gcloud.workload_identity import _send_json_abs
from tests.test_connectors_gcloud_workload_identity import _OPERATOR, _TARGET, connector

__all__ = ["connector"]

_URL = "https://storage.googleapis.com/storage/v1/b"
_POOL_URL = "https://iam.googleapis.com/v1/projects/my-project/locations/global/pools/p1"


async def _create(conn: GcloudConnector) -> Any:
    return await conn._post_json_abs(
        _TARGET, f"{_URL}?project=p", operator=_OPERATOR, json_body={"name": "b1"}
    )


async def _delete(conn: GcloudConnector) -> Any:
    return await _send_json_abs(conn, _TARGET, "DELETE", _POOL_URL, operator=_OPERATOR)


async def _refused_status(call: Awaitable[Any]) -> int:
    """The HTTP status of the error that *call* ends with."""
    try:
        await call
    except httpx.HTTPStatusError as refused:
        return refused.response.status_code
    raise AssertionError("the call ended without an HTTP error")


def _answers(*responses: httpx.Response) -> Any:
    """Answer each request with the next of *responses*."""
    queue = list(responses)
    return lambda _request: queue.pop(0)


def _slash_redirect(url: str, query: str = "") -> httpx.Response:
    """A 307 to the same path with a slash at the end, and the same query."""
    return httpx.Response(307, headers={"Location": f"{url}/{query}"})


@pytest.mark.asyncio
async def test_bucket_create_401_then_307_sends_two_posts(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        first = mock.post(_URL).mock(
            side_effect=_answers(httpx.Response(401), _slash_redirect(_URL, "?project=p"))
        )
        slash = mock.post(f"{_URL}/").respond(200, json={"name": "b1"})
        status = await _refused_status(_create(connector))

    assert status == 307
    assert first.call_count + slash.call_count == 2
    assert slash.call_count == 0


@pytest.mark.asyncio
async def test_bucket_create_307_then_401_sends_two_posts(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        first = mock.post(_URL).mock(side_effect=_answers(_slash_redirect(_URL, "?project=p")))
        slash = mock.post(f"{_URL}/").respond(401)
        status = await _refused_status(_create(connector))

    assert status == 401
    assert first.call_count == 1
    assert slash.call_count == 1


@pytest.mark.asyncio
async def test_bucket_create_401_then_success_sends_two_posts(connector: GcloudConnector) -> None:
    with respx.mock() as mock:
        route = mock.post(_URL).mock(
            side_effect=_answers(httpx.Response(401), httpx.Response(200, json={"name": "b1"}))
        )
        result = await _create(connector)

    assert result == {"name": "b1"}
    assert route.call_count == 2


@pytest.mark.asyncio
async def test_delete_401_then_307_sends_two_deletes(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        first = mock.delete(_POOL_URL).mock(
            side_effect=_answers(httpx.Response(401), _slash_redirect(_POOL_URL))
        )
        slash = mock.delete(f"{_POOL_URL}/").respond(200, json={})
        status = await _refused_status(_delete(connector))

    assert status == 307
    assert first.call_count == 2
    assert slash.call_count == 0


@pytest.mark.asyncio
async def test_delete_307_then_401_sends_two_deletes(connector: GcloudConnector) -> None:
    with respx.mock(assert_all_called=False) as mock:
        first = mock.delete(_POOL_URL).mock(side_effect=_answers(_slash_redirect(_POOL_URL)))
        slash = mock.delete(f"{_POOL_URL}/").respond(401)
        status = await _refused_status(_delete(connector))

    assert status == 401
    assert first.call_count == 1
    assert slash.call_count == 1
