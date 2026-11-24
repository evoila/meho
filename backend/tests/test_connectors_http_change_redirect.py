# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect after a change is handled by what its HTTP code means.

A change (PUT, POST, PATCH, DELETE) is sent at most twice, and the second send
goes only to the same path after a 307 or 308. Every other redirect ends the
call, and the result says what the code means. Reads keep following redirects
on the same origin. Names and addresses are made up; the transport is mocked.
"""

from __future__ import annotations

import json
import types
from typing import Any
from unittest.mock import patch
from uuid import UUID

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.adapters.http import HttpConnector, _SameOriginRedirectClient
from meho_backplane.operations._errors import is_redirect_on_change
from meho_backplane.operations.dispatcher import _classify_http_status_error
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME

_BASE = "https://svc.example.com"
_CHANGE_METHODS = ["PUT", "POST", "PATCH", "DELETE"]


class _Server:
    """A mocked target that records each request and answers from a function."""

    def __init__(self, answer: Any) -> None:
        self.seen: list[tuple[str, str]] = []
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path))
        return self._answer(request)  # type: ignore[no-any-return]


async def _send(server: _Server, method: str, path: str) -> httpx.Response:
    async with _SameOriginRedirectClient(
        base_url=_BASE, transport=httpx.MockTransport(server)
    ) as client:
        return await client.request(method, path, json={"name": "widget"})


def _redirect(status: int, location: str) -> Any:
    return lambda request: httpx.Response(status, headers={"Location": location})


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _CHANGE_METHODS)
@pytest.mark.parametrize("status", [301, 302, 303])
async def test_change_gets_one_request_on_301_302_303(method: str, status: int) -> None:
    server = _Server(_redirect(status, "/jobs/42"))
    resp = await _send(server, method, "/jobs")
    assert resp.status_code == status
    assert server.seen == [(method, "/jobs")]


@pytest.mark.asyncio
@pytest.mark.parametrize("method", _CHANGE_METHODS)
@pytest.mark.parametrize("status", [307, 308])
@pytest.mark.parametrize("location", ["/items/7/", "/ITEMS/7"])
async def test_change_follows_307_308_to_the_same_path(
    method: str, status: int, location: str
) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/items/7":
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, json={"done": True})

    server = _Server(answer)
    resp = await _send(server, method, "/items/7")
    assert resp.status_code == 200
    assert [m for m, _ in server.seen] == [method, method]


@pytest.mark.asyncio
@pytest.mark.parametrize("location", ["/items/8", "/other/7", "https://svc.example.com/items/7/x"])
async def test_change_307_to_another_path_is_not_sent(location: str) -> None:
    server = _Server(_redirect(307, location))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 307
    assert server.seen == [("PUT", "/items/7")]


@pytest.mark.asyncio
async def test_change_307_with_another_query_is_not_sent() -> None:
    server = _Server(_redirect(307, "/items/7?force=1"))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 307
    assert len(server.seen) == 1


@pytest.mark.asyncio
async def test_change_308_to_itself_is_sent_once() -> None:
    server = _Server(_redirect(308, "/items/7"))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 308
    assert len(server.seen) == 1


@pytest.mark.asyncio
async def test_change_307_chain_is_sent_at_most_twice() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        target = "/a/" if request.url.path == "/a" else "/a"
        return httpx.Response(307, headers={"Location": target})

    server = _Server(answer)
    resp = await _send(server, "PUT", "/a")
    assert resp.status_code == 307
    assert server.seen == [("PUT", "/a"), ("PUT", "/a/")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["https://other.example.com/items/7", "https://svc.example.com:8443/items/7"]
)
async def test_change_307_to_another_origin_is_not_sent(location: str) -> None:
    server = _Server(_redirect(307, location))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 307
    assert len(server.seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307, 308])
async def test_read_still_follows_a_same_origin_redirect(status: int) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list":
            return httpx.Response(status, headers={"Location": "/list/"})
        return httpx.Response(200, json={"items": []})

    server = _Server(answer)
    resp = await _send(server, "GET", "/list")
    assert resp.status_code == 200
    assert server.seen == [("GET", "/list"), ("GET", "/list/")]


@pytest.mark.asyncio
async def test_read_redirect_loop_still_raises() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        target = "/a/" if request.url.path == "/a" else "/a"
        return httpx.Response(302, headers={"Location": target})

    with pytest.raises(httpx.TooManyRedirects):
        await _send(_Server(answer), "GET", "/a")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sent", "location"),
    [
        ("/items/a%2Fb", "/items/a/b"),
        ("/items/a/b", "/items/a%2Fb"),
        ("/items/a%2Fb", "/items/a%2fb/"),
    ],
)
async def test_change_307_compares_the_encoded_path(sent: str, location: str) -> None:
    seen: list[bytes] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.raw_path)
        if len(seen) == 1:
            return httpx.Response(307, headers={"Location": location})
        return httpx.Response(200, json={})

    async with _SameOriginRedirectClient(
        base_url=_BASE, transport=httpx.MockTransport(answer)
    ) as client:
        await client.request("PUT", httpx.URL(_BASE + sent), json={"name": "widget"})

    # A slash inside a name (%2F) and a slash between two names differ: one
    # request only. A change of letter case in the escape is the same path.
    expected = 2 if location.lower().removesuffix("/") == sent.lower() else 1
    assert len(seen) == expected


@pytest.mark.asyncio
async def test_change_307_that_changes_only_the_fragment_is_not_sent_again() -> None:
    server = _Server(_redirect(307, "/items/7#part"))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 307
    assert server.seen == [("PUT", "/items/7")]


@pytest.mark.asyncio
async def test_followed_redirect_is_kept_in_history() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/items/7":
            return httpx.Response(307, headers={"Location": "/items/7/"})
        return httpx.Response(401)

    resp = await _send(_Server(answer), "PUT", "/items/7")
    assert resp.status_code == 401
    assert [r.status_code for r in resp.history] == [307]
    plain = await _send(_Server(lambda request: httpx.Response(200)), "PUT", "/items/7")
    assert plain.history == []


def _operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="tok",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


def _target() -> Any:
    return types.SimpleNamespace(
        name="svc",
        host="svc.example.com",
        port=443,
        id="11111111-1111-1111-1111-111111111111",
        tenant_id="00000000-0000-0000-0000-000000000000",
        auth_model="impersonation",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
        extras={},
    )


class _Conn(HttpConnector):
    product = "svc-test"

    async def auth_headers(self, target: Any, operator: Operator) -> dict[str, str]:
        return {"Authorization": "Bearer tok"}

    async def fingerprint(self, target: Any, operator: Any = None) -> Any:  # type: ignore[override]
        raise NotImplementedError

    async def probe(self, target: Any) -> Any:
        raise NotImplementedError

    async def execute(self, target: Any, op_id: str, params: dict[str, Any]) -> Any:  # type: ignore[override]
        raise NotImplementedError


def _recording() -> tuple[list[tuple[str, int, str]], Any]:
    recorded: list[tuple[str, int, str]] = []

    def fake_record(start: Any, *, method: str, response: Any, **_: Any) -> None:
        if start is None:  # the real recorder records nothing without a start
            return
        recorded.append((method, response.status_code, response.request.url.path))

    patches = (
        patch(
            "meho_backplane.connectors.adapters.http.flight_recorder_capture.span_start",
            return_value=object(),
        ),
        patch(
            "meho_backplane.connectors.adapters.http.flight_recorder_capture.record_vendor_call",
            side_effect=fake_record,
        ),
    )
    return recorded, patches


@pytest.mark.asyncio
async def test_trace_of_a_write_has_each_request_once() -> None:
    recorded, (start_patch, record_patch) = _recording()
    conn = _Conn()
    with start_patch, record_patch:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put("/items/7").respond(307, headers={"Location": "/items/7/"})
            mock.put("/items/7/").respond(200, json={"ok": True})
            await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert recorded == [("PUT", 307, "/items/7"), ("PUT", 200, "/items/7/")]


@pytest.mark.asyncio
async def test_trace_of_an_options_redirect_names_the_get_that_followed() -> None:
    recorded, (start_patch, record_patch) = _recording()
    conn = _Conn()
    with start_patch, record_patch:
        async with respx.mock(base_url=_BASE) as mock:
            mock.options("/items").respond(303, headers={"Location": "/items/"})
            mock.get("/items/").respond(200, json={})
            await conn._request_json(_target(), "OPTIONS", "/items", operator=_operator())
    await conn.aclose()

    assert recorded == [("OPTIONS", 303, "/items"), ("GET", 200, "/items/")]


@pytest.mark.asyncio
async def test_direct_client_call_puts_no_redirect_into_the_trace() -> None:
    # A login goes to the client directly and stays out of the trace, also
    # when the target redirects it.
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(307, headers={"Location": "/login/"})
        return httpx.Response(200, json={})

    recorded, (start_patch, record_patch) = _recording()
    with start_patch, record_patch:
        await _send(_Server(answer), "POST", "/login")

    assert recorded == []


def _status_error(method: str, status: int, location: str | None) -> httpx.HTTPStatusError:
    request = httpx.Request(method, f"{_BASE}/items/7")
    headers = {"Location": location} if location else {}
    response = httpx.Response(status, headers=headers, request=request)
    return httpx.HTTPStatusError("redirect", request=request, response=response)


def _classify(
    exc: httpx.HTTPStatusError, *, change: bool = True, op_sends_secret: bool = False
) -> Any:
    return _classify_http_status_error(
        http_exc=exc,
        op_id="svc.item.update",
        connector_instance=None,
        target=None,
        duration_ms=1.0,
        changes_target=change,
        op_sends_secret=op_sends_secret,
    )


def _real_status_error(status: int, location: str) -> httpx.HTTPStatusError:
    """The error that ``raise_for_status`` raises for a real redirect answer."""
    request = httpx.Request("POST", f"{_BASE}/jobs?run=1")
    response = httpx.Response(
        status, headers={"Location": location}, text=f"See {location}", request=request
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_result_keeps_a_ticket_in_the_address_out_of_every_field(status: int) -> None:
    exc = _real_status_error(status, "/jobs/42?ticket=one-time-ticket-123")
    # httpx puts the full address into its own message.
    assert "one-time-ticket-123" in str(exc)

    result = _classify(exc)

    dumped = json.dumps(result.model_dump(mode="json"))
    assert "one-time-ticket-123" not in dumped
    assert "https://svc.example.com/jobs/42" in result.error
    assert result.extras["exception_message"] == (
        f"HTTP {status} redirect after POST https://svc.example.com/jobs"
    )


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_result_leaves_out_the_address_for_an_operation_that_sends_a_secret(status: int) -> None:
    exc = _real_status_error(status, "/vault/the-secret-value/result")

    result = _classify(exc, op_sends_secret=True)

    dumped = json.dumps(result.model_dump(mode="json"))
    assert "the-secret-value" not in dumped
    assert "does not show, because this call sends a secret" in result.error


@pytest.mark.parametrize("status", [307, 308])
def test_result_for_307_308_says_the_target_did_not_run_the_call(status: int) -> None:
    result = _classify(_status_error("PUT", status, "/items/8?ticket=abc"))
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == status
    assert "did not run this call" in result.error
    assert "https://svc.example.com/items/8" in result.error
    assert "ticket" not in result.error
    assert "not the approved call" in result.error
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME


def test_result_for_303_says_the_change_ran() -> None:
    result = _classify(_status_error("POST", 303, "/jobs/42"))
    assert "target says the change ran" in result.error
    assert "https://svc.example.com/jobs/42" in result.error
    assert "did not read it" in result.error
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME


@pytest.mark.parametrize("status", [301, 302])
def test_result_for_301_302_says_it_could_not_find_out(status: int) -> None:
    result = _classify(_status_error("DELETE", status, "/items/7/"))
    assert "could not find out if the change ran" in result.error
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME


def test_redirect_on_a_read_keeps_the_generic_result() -> None:
    exc = _status_error("GET", 302, "/items/7/")
    assert not is_redirect_on_change(exc)
    result = _classify(exc, change=False)
    assert result.error == "connector_error: HTTPStatusError"
    assert "execution_outcome" not in result.extras


def test_redirect_without_location_keeps_the_generic_result() -> None:
    exc = _status_error("PUT", 307, None)
    assert not is_redirect_on_change(exc)
    assert _classify(exc).error.startswith("connector_error: HTTPStatusError")
