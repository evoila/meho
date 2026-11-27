# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect MEHO does not follow: the answer names only host names.

A small typed HTTP connector runs one read operation through the real
dispatcher, with the tenant's trace on, against a local HTTP server. The
server answers with a redirect. Each case puts made-up values into the parts
of the ``Location`` address the vendor chooses: the path, the query, the
fragment, the user part and the port text. Most cases first send MEHO
through one followed redirect on the same server, whose address also holds
made-up values.

The tests check the answer, the audit row, the stored trace and its spans,
and the log output at DEBUG and above. None of them holds a made-up value.
The answer still holds the HTTP status and the host name of the address.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from meho_backplane.connectors._shared.log_host import UNREADABLE_LOCATION_MESSAGE
from meho_backplane.connectors.adapters.http import _SameOriginRedirectClient
from meho_backplane.flight_recorder.capture import FIRST_REQUEST_URL_EXTENSION
from meho_backplane.operations._errors import result_connector_error
from tests._http_redirect_harness import (  # noqa: F401 -- the autouse fixtures apply by import
    _AGENT_STORE,
    _OP_ID,
    _OTHER_HOST,
    _START,
    _agent_trace_store,
    _assert_absent,
    _made_up,
    _reset_module_state,
    _run,
    _settings_env,
    _stub_embedding,
)


def _refusal_event(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next(e for e in events if e["event"] == "connector_redirect_not_followed_cross_origin")


@pytest.mark.asyncio
async def test_refused_redirect_after_a_followed_hop_names_only_host_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hop_path, hop_query = _made_up(), _made_up()
    user, password, path, query, fragment = (_made_up() for _ in range(5))
    hop = f"/{hop_path}?q={hop_query}"
    location = f"http://{user}:{password}@{_OTHER_HOST}/{path}?q={query}#{fragment}"
    routes = {_START: {"Location": hop}, hop: {"Location": location}}

    result, places, events = await _run(caplog, routes)

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == 302
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert _refusal_event(events)["reason"] == "other host"
    _assert_absent([hop_path, hop_query, user, password, path, query, fragment], places)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("authority", "host", "reason"),
    [
        ("[::1]:8443", "::1", "other host"),
        ("127.0.0.1:1", "127.0.0.1", "other port"),
    ],
)
async def test_refused_redirect_names_only_the_host_name(
    caplog: pytest.LogCaptureFixture,
    authority: str,
    host: str,
    reason: str,
) -> None:
    path, query, fragment = _made_up(), _made_up(), _made_up()
    routes = {_START: {"Location": f"http://{authority}/{path}?q={query}#{fragment}"}}

    result, places, events = await _run(caplog, routes)

    assert result.extras["http_status"] == 302
    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {host}, not followed."
    )
    assert _refusal_event(events)["reason"] == reason
    _assert_absent([path, query, fragment], places)


@pytest.mark.asyncio
async def test_redirect_to_https_on_the_same_host_names_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = _made_up()
    routes = {_START: {"Location": f"https://127.0.0.1/{path}"}}

    result, places, events = await _run(caplog, routes)

    assert result.extras["exception_message"] == (
        "Redirect response 302 from 127.0.0.1 to 127.0.0.1, not followed."
    )
    assert _refusal_event(events)["reason"] == "other scheme"
    _assert_absent([path], places)


@pytest.mark.asyncio
async def test_unreadable_port_after_a_followed_hop_gives_one_fixed_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hop_path, port, path, query = _made_up(), _made_up(), _made_up(), _made_up()
    hop = f"/{hop_path}"
    routes = {
        _START: {"Location": hop},
        hop: {"Location": f"http://{_OTHER_HOST}:{port}/{path}?q={query}"},
    }

    result, places, events = await _run(caplog, routes)

    assert result.status == "error"
    assert result.extras["exception_message"] == UNREADABLE_LOCATION_MESSAGE
    assert _refusal_event(events)["reason"] == "unreadable address"
    _assert_absent([hop_path, port, path, query], places)


def test_redirect_without_location_names_only_the_host() -> None:
    """A 3xx without a Location header: the text has the status and the request host only."""
    path, query = _made_up(), _made_up()
    request = httpx.Request("GET", f"https://appliance.test.invalid/{path}?q={query}")
    response = httpx.Response(304, request=request)
    with pytest.raises(httpx.HTTPStatusError) as error:
        response.raise_for_status()

    result = result_connector_error(_OP_ID, error.value, 1.0)

    assert result.extras["exception_message"] == (
        "Redirect response 304 from appliance.test.invalid, with no Location header."
    )
    assert result.extras["http_status"] == 304
    _assert_absent([path, query], {"answer": result.model_dump_json()})


@pytest.mark.asyncio
async def test_followed_hop_answer_names_the_first_address_for_the_trace() -> None:
    """After a followed hop, the answer carries the address MEHO asked for, for the trace."""
    hop = f"/{_made_up()}"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == _START:
            return httpx.Response(302, headers={"Location": hop})
        return httpx.Response(200, json={})

    async with _SameOriginRedirectClient(
        base_url="https://appliance.test.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        followed = await client.get(_START)
        direct = await client.get(hop)

    assert followed.extensions[FIRST_REQUEST_URL_EXTENSION] == httpx.URL(
        f"https://appliance.test.invalid{_START}"
    )
    assert FIRST_REQUEST_URL_EXTENSION not in direct.extensions


@pytest.mark.asyncio
async def test_followed_hop_whose_body_ends_early_names_only_the_first_address(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hop_path, hop_query = _made_up(), _made_up()
    hop = f"/{hop_path}?q={hop_query}"
    routes = {_START: {"Location": hop}}

    result, places, _events = await _run(
        caplog, routes, status="307 Temporary Redirect", short_body=frozenset({hop})
    )

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_timeout"
    assert _AGENT_STORE.values, "the agent got no copy of the trace"
    assert f'{_START}"' in places["stored trace"]
    _assert_absent([hop_path, hop_query], places)
