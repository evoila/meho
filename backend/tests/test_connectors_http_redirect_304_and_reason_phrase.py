# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A 304 answer, and the reason phrase of an answer after a redirect.

A small typed HTTP connector runs one read operation through the real
dispatcher against a local HTTP server.

- A ``304 Not Modified`` asks for no redirect. Its answer and its trace hold
  what the vendor sent, as for any other status. Every other 3xx that MEHO
  does not follow has one fixed sentence in place of the vendor's text.
- The reason phrase of a status line is the vendor's choice. After a
  followed redirect, the error answer holds the fixed phrase of the status
  number, or the number only. A refused 3xx holds no reason phrase.
"""

from __future__ import annotations

import secrets

import pytest

from meho_backplane.connectors._shared.log_host import REDIRECT_TEXT_WITHHELD
from tests._http_redirect_harness import (  # noqa: F401 -- the autouse fixtures apply by import
    _OTHER_HOST,
    _START,
    _agent_trace_store,
    _assert_absent,
    _httpx_logger_on,
    _made_up,
    _reset_module_state,
    _run,
    _settings_env,
    _stub_embedding,
    _vendor_call_bodies,
)


def _phrase() -> str:
    """A made-up reason phrase for a status line, built at run time."""
    return f"Moved zq{secrets.token_hex(14)}"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [b"", b"short"], ids=["no-body", "short-body"])
async def test_not_modified_answer_keeps_what_the_vendor_sent(
    caplog: pytest.LogCaptureFixture, body: bytes
) -> None:
    routes = {_START: {}}

    result, _places, _events = await _run(
        caplog, routes, route_status={_START: "304 Not Modified"}, route_body={_START: body}
    )

    assert result.status == "error"
    assert result.extras["http_status"] == 304
    assert result.extras["exception_message"] == (
        "Redirect response 304 from 127.0.0.1, with no Location header."
    )
    # A 304 has no body in HTTP, so the vendor sent no text to pass on.
    assert result.extras["upstream_message"] is None
    assert "upstream_fault" not in result.extras
    assert result.audit_id is not None
    assert await _vendor_call_bodies(result.audit_id) == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_line",
    [
        "300 Multiple Choices",
        "301 Moved Permanently",
        "302 Found",
        "303 See Other",
        "307 Temporary Redirect",
        "308 Permanent Redirect",
    ],
)
async def test_refused_redirect_has_the_fixed_sentence_in_answer_and_span(
    caplog: pytest.LogCaptureFixture, status_line: str
) -> None:
    path = _made_up()
    routes = {
        _START: {"Location": f"http://{_OTHER_HOST}/{path}", "Content-Type": "application/json"}
    }
    body = f'{{"_typeName": "NotFound", "faultstring": "See /{path}"}}'.encode()

    result, places, _events = await _run(
        caplog, routes, route_status={_START: status_line}, route_body={_START: body}
    )

    code = int(status_line.split()[0])
    assert result.extras["http_status"] == code
    assert result.extras["exception_message"] == (
        f"Redirect response {code} from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert result.extras["upstream_message"] == REDIRECT_TEXT_WITHHELD
    assert "upstream_fault" not in result.extras
    assert result.audit_id is not None
    assert await _vendor_call_bodies(result.audit_id) == [REDIRECT_TEXT_WITHHELD]
    _assert_absent([path], places)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "text"),
    [
        (404, "Client error '404 Not Found'"),
        (429, "Client error '429 Too Many Requests'"),
        (500, "Server error '500 Internal Server Error'"),
        (599, "Server error '599'"),
    ],
)
@pytest.mark.parametrize("trace", [True, False], ids=["trace-on", "trace-off"])
async def test_error_after_a_followed_redirect_has_the_fixed_reason_phrase(
    caplog: pytest.LogCaptureFixture, code: int, text: str, trace: bool
) -> None:
    phrase, hop = _phrase(), f"/{_made_up()}"
    routes = {_START: {"Location": f"http://127.0.0.1:{{port}}{hop}"}, hop: {}}

    result, places, _events = await _run(
        caplog,
        routes,
        trace=trace,
        route_status={_START: "307 Temporary Redirect", hop: f"{code} {phrase}"},
    )

    assert result.status == "error"
    assert result.extras["http_status"] == code
    assert result.extras["exception_message"] == (
        f"{text} from 127.0.0.1, after a redirect on the same host."
    )
    _assert_absent([phrase], places)


@pytest.mark.asyncio
@pytest.mark.parametrize("trace", [True, False], ids=["trace-on", "trace-off"])
async def test_refused_redirect_holds_no_reason_phrase(
    caplog: pytest.LogCaptureFixture, trace: bool
) -> None:
    phrase = _phrase()
    routes = {_START: {"Location": f"http://{_OTHER_HOST}/next"}}

    result, places, _events = await _run(
        caplog, routes, trace=trace, route_status={_START: f"302 {phrase}"}
    )

    assert result.extras["exception_message"] == (
        f"Redirect response 302 from 127.0.0.1 to {_OTHER_HOST}, not followed."
    )
    assert result.extras["upstream_message"] == REDIRECT_TEXT_WITHHELD
    _assert_absent([phrase], places)
