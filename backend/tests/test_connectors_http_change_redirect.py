# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""A redirect after a change is handled by what its HTTP code means.

A change (PUT, POST, PATCH, DELETE) is sent at most twice, and the second send
goes only to the same path after a 307 or 308. Every other redirect ends the
call, and the result says what the code means. Reads keep following redirects
on the same origin. Each followed redirect passes the same block check as the
first send. Names and addresses are made up; the transport is mocked.
"""

from __future__ import annotations

import contextlib
import json
import types
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx
import pytest
import respx
from structlog.testing import capture_logs

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.auth.principal_block import PrincipalBlockedStepError
from meho_backplane.connectors._shared.login_mark import mark_as_login
from meho_backplane.connectors.adapters import http as http_adapter
from meho_backplane.connectors.adapters.http import (
    HttpConnector,
    _SameOriginRedirectClient,
    change_sent_once_before,
    login_request,
)
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.flight_recorder import capture
from meho_backplane.operations._errors import is_redirect_on_change, result_connector_error
from meho_backplane.operations.dispatcher import _classify_http_status_error
from meho_backplane.operations.uncertain_outcome import UNCERTAIN_OUTCOME
from meho_backplane.settings import get_settings

_BASE = "https://svc.example.com"
_CHANGE_METHODS = ["PUT", "POST", "PATCH", "DELETE"]


def _core_client(**kwargs: Any) -> _SameOriginRedirectClient:
    """A pooled client with the change rule, as a connector that comes with MEHO gets it."""
    client = _SameOriginRedirectClient(**kwargs)
    client.change_rule = True
    return client


class _Server:
    """A mocked target that records each request and answers from a function."""

    def __init__(self, answer: Any) -> None:
        self.seen: list[tuple[str, str]] = []
        self._answer = answer

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append((request.method, request.url.path))
        return self._answer(request)  # type: ignore[no-any-return]


@contextlib.contextmanager
def _operation_request() -> Iterator[None]:
    """Send as the operation's own request, as ``_post_json`` does."""
    token = http_adapter._operation_request.set(True)
    try:
        yield
    finally:
        http_adapter._operation_request.reset(token)


def _client(server: _Server) -> _SameOriginRedirectClient:
    """A client with the change rule, as a connector that comes with MEHO gets it."""
    client = _SameOriginRedirectClient(base_url=_BASE, transport=httpx.MockTransport(server))
    client.change_rule = True
    return client


async def _send(
    server: _Server, method: str, path: str, *, operation: bool = True
) -> httpx.Response:
    """Send one request: the operation's own request, or else a named login."""
    async with _client(server) as client:
        if not operation:
            with login_request():
                return await client.request(method, path, json={"name": "widget"})
        with _operation_request():
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

    async with _core_client(base_url=_BASE, transport=httpx.MockTransport(answer)) as client:
        with _operation_request():
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


@pytest.mark.asyncio
async def test_change_307_that_adds_only_a_login_part_is_not_sent_again() -> None:
    # A user name in the address is not part of what HTTP sends as the
    # address: origin, path and query are the same, so it is the same address.
    server = _Server(_redirect(307, "https://someone@svc.example.com/items/7"))
    resp = await _send(server, "PUT", "/items/7")
    assert resp.status_code == 307
    assert server.seen == [("PUT", "/items/7")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location",
    ["/keys/redirect-secret-value", "https://other.example.com/keys/redirect-secret-value"],
)
async def test_log_line_of_a_change_redirect_keeps_only_the_host_name(location: str) -> None:
    # The sent path and the path the target names can both carry a secret
    # value of the call. The log line of a change names the host names only.
    server = _Server(_redirect(307, location))
    with capture_logs() as captured:
        async with _core_client(base_url=_BASE, transport=httpx.MockTransport(server)) as client:
            with _operation_request():
                await client.request("PUT", "/keys/sent-secret-value", json={"name": "widget"})

    warnings = [e for e in captured if e["event"].startswith("connector_redirect_not_followed")]
    assert len(warnings) == 1
    dumped = json.dumps(captured, default=str)
    assert "sent-secret-value" not in dumped
    assert "redirect-secret-value" not in dumped
    assert warnings[0]["from_url"] == "svc.example.com"


@pytest.mark.asyncio
async def test_a_repeat_after_login_sends_a_change_once_more_at_most() -> None:
    # The change was sent once before the login ended. Its repeat is the
    # second and last send: a 307 to the same path is not followed.
    conn = _Conn()
    with change_sent_once_before():
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            first = mock.put("/items/7").respond(307, headers={"Location": "/items/7/"})
            second = mock.put("/items/7/").respond(200, json={"ok": True})
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert caught.value.response.status_code == 307
    assert first.call_count == 1
    assert second.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_login_during_a_repeat_follows_a_same_origin_redirect(status: int) -> None:
    # A fresh login before the repeat is not the operation's own request. It
    # follows a redirect on the same origin, as a read does.
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session":
            return httpx.Response(status, headers={"Location": "/api/session/"})
        return httpx.Response(200, json={"token": "made-up"})

    server = _Server(answer)
    with change_sent_once_before():
        resp = await _send(server, "POST", "/api/session", operation=False)
    assert resp.status_code == 200
    again = "POST" if status in (307, 308) else "GET"
    assert server.seen == [("POST", "/api/session"), (again, "/api/session/")]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", ["/api/session/", "/api/v2/session"])
async def test_a_login_follows_a_same_origin_redirect_as_a_read_does(
    status: int, location: str
) -> None:
    # A login is a request of the pooled client that is not the operation's
    # own request: the change rule does not apply to it.
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session":
            return httpx.Response(status, headers={"Location": location})
        return httpx.Response(200, json={"token": "made-up"})

    server = _Server(answer)
    resp = await _send(server, "POST", "/api/session", operation=False)
    assert resp.status_code == 200
    again = "POST" if status in (307, 308) else "GET"
    assert server.seen == [("POST", "/api/session"), (again, location)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "location", ["https://other.example.com/api/session", "http://svc.example.com/api/session"]
)
async def test_a_login_never_follows_a_redirect_to_another_origin(location: str) -> None:
    server = _Server(_redirect(307, location))
    resp = await _send(server, "POST", "/api/session", operation=False)
    assert resp.status_code == 307
    assert server.seen == [("POST", "/api/session")]


@pytest.mark.asyncio
async def test_the_operation_change_keeps_its_rule_for_a_301_to_the_same_path() -> None:
    # The operation's own PUT through ``_post_json`` follows no 301, also not
    # to its own path with a slash.
    conn = _Conn()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        first = mock.put("/items/7").respond(301, headers={"Location": "/items/7/"})
        second = mock.route(path="/items/7/").respond(200, json={"ok": True})
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()
    assert caught.value.response.status_code == 301
    assert (first.call_count, second.call_count) == (1, 0)


@pytest.mark.asyncio
async def test_the_operation_change_follows_a_307_to_its_path_in_lower_case() -> None:
    conn = _Conn()
    async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
        first = mock.put("/Items/7", params={"mode": "a"}).respond(
            307, headers={"Location": "/items/7?mode=a"}
        )
        second = mock.put("/items/7", params={"mode": "a"}).respond(200, json={"ok": True})
        await conn._post_json(
            _target(), "/Items/7", operator=_operator(), verb="PUT", params={"mode": "a"}
        )
    await conn.aclose()
    assert (first.call_count, second.call_count) == (1, 1)


@pytest.mark.asyncio
async def test_a_repeat_after_login_still_follows_a_read_redirect() -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/list":
            return httpx.Response(307, headers={"Location": "/list/"})
        return httpx.Response(200, json={"items": []})

    server = _Server(answer)
    with change_sent_once_before():
        resp = await _send(server, "GET", "/list")
    assert resp.status_code == 200
    assert server.seen == [("GET", "/list"), ("GET", "/list/")]


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
async def test_trace_of_an_options_redirect_names_the_get_that_timed_out() -> None:
    recorded, (start_patch, record_patch) = _recording()
    errors: list[str] = []
    error_paths: list[str] = []

    def record_error(_start: Any, *, method: str, exc: Exception) -> None:
        errors.append(method)
        error_paths.append(exc.request.url.path)  # type: ignore[attr-defined]

    conn = _Conn()
    with (
        start_patch,
        record_patch,
        patch.object(capture, "record_vendor_call_error", side_effect=record_error),
    ):
        async with respx.mock(base_url=_BASE) as mock:
            mock.options("/items").respond(303, headers={"Location": "/items/"})
            mock.get("/items/").mock(side_effect=httpx.ReadTimeout("made-up timeout"))
            with pytest.raises(httpx.ReadTimeout):
                await conn._request_json(_target(), "OPTIONS", "/items", operator=_operator())
    await conn.aclose()

    assert recorded == [("OPTIONS", 303, "/items")]
    assert errors == ["GET"]
    # The error entry names the address MEHO asked for.
    assert error_paths == ["/items"]


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
        await _send(_Server(answer), "POST", "/login", operation=False)

    assert recorded == []


class _BlockAfterFirstAnswer:
    """The block check of the caller's login, which refuses once the first answer came back.

    It stands in for main's check before each send
    (``raise_if_send_refused``, run by the client's request hook), so the
    caller is blocked between the first answer and the next request.
    """

    def __init__(self) -> None:
        self.blocked = False
        self.checks = 0

    async def check(self) -> None:
        self.checks += 1
        if self.blocked:
            raise PrincipalBlockedStepError(unavailable=False)

    def redirect(self, location: str) -> Any:
        def answer(_request: httpx.Request) -> httpx.Response:
            self.blocked = True
            return httpx.Response(307, headers={"Location": location})

        return answer

    def patched(self) -> Any:
        return patch(
            "meho_backplane.connectors.adapters.http.raise_if_send_refused",
            side_effect=self.check,
        )


@pytest.mark.asyncio
async def test_login_blocked_before_a_followed_change_redirect_gets_no_second_request() -> None:
    block = _BlockAfterFirstAnswer()
    conn = _Conn()
    with block.patched():
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            first = mock.put("/items/7").mock(side_effect=block.redirect("/items/7/"))
            second = mock.put("/items/7/").respond(200, json={"ok": True})
            with pytest.raises(PrincipalBlockedStepError):
                await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert first.call_count == 1
    assert second.call_count == 0


@pytest.mark.asyncio
async def test_login_blocked_before_a_followed_read_redirect_gets_no_second_request() -> None:
    block = _BlockAfterFirstAnswer()
    conn = _Conn()
    with block.patched():
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            first = mock.get("/list").mock(side_effect=block.redirect("/list/"))
            second = mock.get("/list/").respond(200, json={"items": []})
            with pytest.raises(PrincipalBlockedStepError):
                await conn._request_json(_target(), "GET", "/list", operator=_operator())
    await conn.aclose()

    assert first.call_count == 1
    assert second.call_count == 0


_KC_BASE = "https://kc.example.test"


async def _kc_loader(_target: KeycloakTargetLike, _operator: Operator) -> KeycloakAdminCredentials:
    return KeycloakClientCredentials(client_id="meho-admin", client_secret="stub-secret")


def _kc_target() -> Any:
    return types.SimpleNamespace(
        name="kc",
        host="kc.example.test",
        port=443,
        secret_ref="stub/secret",
        auth_model="shared_service_account",
        extras={"managed_realm": "demo"},
        id="22222222-2222-2222-2222-222222222222",
        tenant_id="00000000-0000-0000-0000-000000000000",
        verify_tls=True,
        tls_ca_pin=None,
        tls_server_name=None,
    )


@pytest.mark.asyncio
async def test_trace_of_a_keycloak_write_has_each_request_and_no_login() -> None:
    # The typed Keycloak write sends its own request. The trace gets the 307
    # and the answer after it. The token request before it (also redirected
    # here) stays out of the trace.
    recorded, (start_patch, record_patch) = _recording()
    conn = KeycloakConnector(credentials_loader=_kc_loader)
    with start_patch, record_patch:
        async with respx.mock(base_url=_KC_BASE) as mock:
            mock.post(url__regex=r".*/openid-connect/token$").respond(
                307, headers={"Location": "/realms/master/protocol/openid-connect/token/"}
            )
            mock.post(url__regex=r".*/openid-connect/token/$").respond(
                200, json={"access_token": "made-up-token", "expires_in": 60}
            )
            mock.put("/admin/realms/demo/groups/7").respond(
                307, headers={"Location": "/admin/realms/demo/groups/7/"}
            )
            write = mock.put("/admin/realms/demo/groups/7/").respond(204)
            result = await conn._write_admin(
                _kc_target(),
                "PUT",
                "/admin/realms/demo/groups/7",
                operator=_operator(),
                json={"name": "widgets"},
            )
    await conn.aclose()

    assert result.status_code == 204
    assert write.call_count == 1
    assert recorded == [
        ("PUT", 307, "/admin/realms/demo/groups/7"),
        ("PUT", 204, "/admin/realms/demo/groups/7/"),
    ]


@pytest.mark.asyncio
async def test_login_blocked_before_a_followed_keycloak_write_redirect_gets_no_second_request() -> (
    None
):
    # An approved Keycloak write sends its own request through ``_send_traced``.
    # The caller's login is blocked after the 307: the target gets one write.
    block = _BlockAfterFirstAnswer()
    conn = KeycloakConnector(credentials_loader=_kc_loader)
    with block.patched():
        async with respx.mock(base_url=_KC_BASE, assert_all_called=False) as mock:
            mock.post(url__regex=r".*/openid-connect/token$").respond(
                200, json={"access_token": "made-up-token", "expires_in": 60}
            )
            first = mock.put("/admin/realms/demo/groups/7").mock(
                side_effect=block.redirect("/admin/realms/demo/groups/7/")
            )
            second = mock.put("/admin/realms/demo/groups/7/").respond(204)
            with pytest.raises(PrincipalBlockedStepError):
                await conn._write_admin(
                    _kc_target(),
                    "PUT",
                    "/admin/realms/demo/groups/7",
                    operator=_operator(),
                    json={"name": "widgets"},
                )
    await conn.aclose()

    assert first.call_count == 1
    assert second.call_count == 0


def _real_capture(op_id: str, tags: tuple[str, ...] = (), params: Any = None) -> Any:
    """Turn on the real trace recorder for one operation; yield its stored entries."""
    scope = capture._CaptureScope(audit_id=uuid.uuid4(), tenant_id=uuid.uuid4(), target_id=None)
    op = capture._OpContext(
        op_id=op_id,
        connector_id="svc-test",
        impl_id=None,
        product=None,
        tags=tags,
        body_paths=(),
        # Set (not None) so the classifier does not read app settings.
        delete_shaped_patterns=(),
        path_values=capture.path_values(params),
    )

    @contextlib.contextmanager
    def running() -> Iterator[list[Any]]:
        scope_token = capture._active_capture_var.set(scope)
        op_token = capture._op_context_var.set(op)
        try:
            yield scope.spans
        finally:
            capture._op_context_var.reset(op_token)
            capture._active_capture_var.reset(scope_token)

    return running()


def _dumped(spans: list[Any]) -> str:
    """Every stored entry as one text, to look for a value that must be hidden."""
    return json.dumps(
        [{"name": span.name, "attributes": span.attributes} for span in spans], default=str
    )


_SECRET_NAME = "sent-secret-value"
_MASKED = f"{_BASE}/keys/[redacted]"


#: A made-up realm name. ``keycloak.realm.update`` puts it into the PUT path,
#: and the full realm it sends in the body holds it too.
_REALM = "mrkrealm5e"


@pytest.mark.asyncio
async def test_trace_of_a_keycloak_realm_update_masks_the_realm_in_the_path() -> None:
    """The handler fills the realm into the path, so the trace masks it there.

    The fixed segments ``admin`` and ``realms`` stay. The realm read before
    the write and the read back after it are masked the same way.
    """
    import meho_backplane.connectors.keycloak.op_classes  # noqa: F401  (declares the class)

    target = _kc_target()
    target.extras = {"managed_realm": _REALM}
    realm_path = f"/admin/realms/{_REALM}"
    conn = KeycloakConnector(credentials_loader=_kc_loader)
    params = {"realm": _REALM, "representation": {"displayName": "plain"}}
    with _real_capture("keycloak.realm.update", params=params) as spans:
        async with respx.mock(base_url=_KC_BASE, assert_all_called=False) as mock:
            mock.post(url__regex=r".*/openid-connect/token$").respond(
                200, json={"access_token": "made-up-token", "expires_in": 60}
            )
            mock.get(realm_path).respond(200, json={"realm": _REALM, "enabled": True})
            write = mock.put(realm_path).respond(204)
            await conn.realm_update(_operator(), target, params)
    await conn.aclose()

    assert write.call_count == 1
    assert write.calls.last.request.content.decode().count(_REALM) >= 1
    urls = [span.attributes["url"] for span in spans]
    assert f"{_KC_BASE}/admin/realms/[redacted]" in urls
    assert all(url == f"{_KC_BASE}/admin/realms/[redacted]" for url in urls), urls
    assert _REALM not in json.dumps([span.attributes.get("url") for span in spans])
    assert _REALM not in json.dumps([span.name for span in spans])
    assert _REALM not in _dumped(spans)


def _secret_capture() -> Any:
    return _real_capture("PUT:/keys/{name}", tags=("secret",), params={"name": _SECRET_NAME})


@pytest.mark.asyncio
async def test_trace_of_an_operation_that_sends_a_secret_masks_the_path_value() -> None:
    conn = _Conn()
    with _secret_capture() as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put(f"/keys/{_SECRET_NAME}").respond(200, json={"ok": True})
            await conn._post_json(
                _target(), f"/keys/{_SECRET_NAME}", operator=_operator(), verb="PUT"
            )
    await conn.aclose()

    assert [(span.name, span.attributes["url"]) for span in spans] == [(f"PUT {_MASKED}", _MASKED)]
    assert _SECRET_NAME not in _dumped(spans)


@pytest.mark.asyncio
async def test_trace_of_an_operation_that_sends_a_secret_masks_the_path_value_after_307() -> None:
    conn = _Conn()
    with _secret_capture() as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put(f"/keys/{_SECRET_NAME}").respond(
                307, headers={"Location": f"/keys/{_SECRET_NAME}/"}
            )
            mock.put(f"/keys/{_SECRET_NAME}/").respond(200, json={"ok": True})
            await conn._post_json(
                _target(), f"/keys/{_SECRET_NAME}", operator=_operator(), verb="PUT"
            )
    await conn.aclose()

    # Both entries name the address MEHO asked for, with the path mask on it.
    assert [(span.name, span.status) for span in spans] == [
        (f"PUT {_MASKED}", "307"),
        (f"PUT {_MASKED}", "200"),
    ]
    assert [span.attributes["url"] for span in spans] == [_MASKED, _MASKED]
    assert _SECRET_NAME not in _dumped(spans)


@pytest.mark.asyncio
async def test_trace_of_an_operation_that_sends_a_secret_masks_the_path_value_on_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    conn = _Conn()
    with _secret_capture() as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put(f"/keys/{_SECRET_NAME}").mock(side_effect=fail)
            with (
                patch("asyncio.sleep", new=AsyncMock()),
                pytest.raises(httpx.ConnectError),
            ):
                await conn._post_json(
                    _target(), f"/keys/{_SECRET_NAME}", operator=_operator(), verb="PUT"
                )
    await conn.aclose()

    assert spans
    assert {span.attributes["url"] for span in spans} == {_MASKED}
    assert _SECRET_NAME not in _dumped(spans)


@pytest.mark.asyncio
async def test_trace_of_another_operation_keeps_the_path() -> None:
    conn = _Conn()
    with _real_capture("PUT:/items/{id}") as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put("/items/7").respond(307, headers={"Location": "/items/7/"})
            mock.put("/items/7/").respond(200, json={"ok": True})
            await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert [span.attributes["url"] for span in spans] == [f"{_BASE}/items/7", f"{_BASE}/items/7"]


@pytest.mark.asyncio
async def test_stored_redirect_entry_of_a_keycloak_write_has_no_body() -> None:
    # The 307 body holds a value that Keycloak's own masking hides. The
    # redirect entry records no body, so the value stays out of the trace.
    conn = KeycloakConnector(credentials_loader=_kc_loader)
    with _real_capture("keycloak.group.update") as spans:
        async with respx.mock(base_url=_KC_BASE) as mock:
            mock.post(url__regex=r".*/openid-connect/token$").respond(
                200, json={"access_token": "made-up-token", "expires_in": 60}
            )
            mock.put("/admin/realms/demo/groups/7").respond(
                307,
                headers={"Location": "/admin/realms/demo/groups/7/"},
                json={"type": "password", "value": "kc-hidden-value"},
            )
            mock.put("/admin/realms/demo/groups/7/").respond(204)
            await conn._write_admin(
                _kc_target(),
                "PUT",
                "/admin/realms/demo/groups/7",
                operator=_operator(),
                json={"name": "widgets"},
            )
    await conn.aclose()

    assert [(span.attributes["method"], span.status) for span in spans] == [
        ("PUT", "307"),
        ("PUT", "204"),
    ]
    assert not spans[0].attributes["response_body"]
    assert "kc-hidden-value" not in _dumped(spans)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The settings every answer reads; the switch stays off unless a test sets it."""
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.delenv("REDIRECT_ON_CHANGE_RESULT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def answer_switch(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Set the switch ``REDIRECT_ON_CHANGE_RESULT`` for one test."""

    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")

    def set_to(on: bool) -> None:
        monkeypatch.setenv("REDIRECT_ON_CHANGE_RESULT", "true" if on else "false")
        get_settings.cache_clear()

    yield set_to
    get_settings.cache_clear()


@pytest.fixture
def answer_switch_on(answer_switch: Any) -> None:
    answer_switch(True)


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


@pytest.mark.usefixtures("answer_switch_on")
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


@pytest.mark.usefixtures("answer_switch_on")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_result_leaves_out_the_address_for_an_operation_that_sends_a_secret(status: int) -> None:
    exc = _real_status_error(status, "/vault/the-secret-value/result")

    result = _classify(exc, op_sends_secret=True)

    dumped = json.dumps(result.model_dump(mode="json"))
    assert "the-secret-value" not in dumped
    assert "does not show, because this call sends a secret" in result.error


@pytest.mark.usefixtures("answer_switch_on")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_result_leaves_out_the_sent_path_for_an_operation_that_sends_a_secret(
    status: int,
) -> None:
    request = httpx.Request("PUT", f"{_BASE}/keys/sent-secret-value")
    response = httpx.Response(status, headers={"Location": "/elsewhere"}, request=request)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()

    result = _classify(caught.value, op_sends_secret=True)

    assert "sent-secret-value" not in json.dumps(result.model_dump(mode="json"))
    assert result.extras["exception_message"] == f"HTTP {status} redirect after PUT {_BASE}"


@pytest.mark.usefixtures("answer_switch_on")
@pytest.mark.parametrize("status", [307, 308])
def test_result_for_307_308_asks_for_a_new_approval(status: int) -> None:
    # A 307 or 308 asks for the same request at another address. The answer
    # says that, makes no claim about what the target ran, and has no
    # uncertain mark.
    result = _classify(_status_error("PUT", status, "/items/8?ticket=abc"))
    assert result.status == "error"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["http_status"] == status
    assert "did not run" not in result.error
    assert "change happened" not in result.error
    assert "https://svc.example.com/items/8" in result.error
    assert "ticket" not in result.error
    assert "not the approved call" in result.error
    assert "execution_outcome" not in result.extras
    assert "retry" not in result.extras


@pytest.mark.usefixtures("answer_switch_on")
def test_result_for_303_names_another_resource_and_claims_no_success() -> None:
    result = _classify(_status_error("POST", 303, "/jobs/42"))
    assert "directed MEHO to another resource" in result.error
    assert "https://svc.example.com/jobs/42" in result.error
    assert "Check on the target whether the change ran" in result.error
    assert "says the change ran" not in result.error
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME


@pytest.mark.usefixtures("answer_switch_on")
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


@pytest.mark.usefixtures("answer_switch_on")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_without_location_after_a_change_is_worded_by_its_code(status: int) -> None:
    # The target got the change, also when its answer names no address.
    exc = _status_error("PUT", status, None)
    assert is_redirect_on_change(exc)
    result = _classify(exc)
    assert "an address it did not name" in result.error
    may_have_run = status in (301, 302, 303)
    assert (result.extras.get("execution_outcome") == UNCERTAIN_OUTCOME) is may_have_run


def test_redirect_on_a_login_is_not_a_redirect_on_change() -> None:
    exc = _status_error("POST", 302, "https://other.example.test/x")
    mark_as_login(exc.request)
    assert not is_redirect_on_change(exc)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_with_the_switch_off_a_change_redirect_gets_the_generic_answer(
    status: int, answer_switch: Any
) -> None:
    # With the switch off the answer is the generic ``connector_error``
    # answer for that status, word for word.
    answer_switch(False)
    exc = _real_status_error(status, "/jobs/42")
    generic = result_connector_error("svc.item.update", exc, 1.0)

    result = _classify(exc)

    assert result == generic
    assert result.error == "connector_error: HTTPStatusError"
    for key in ("execution_outcome", "retry", "remediation", "message_source"):
        assert key not in result.extras


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_with_the_switch_on_a_change_redirect_gets_the_answer_by_its_code(
    status: int, answer_switch: Any
) -> None:
    answer_switch(True)

    result = _classify(_real_status_error(status, "/jobs/42"))

    assert result.error.startswith("connector_error: HTTPStatusError. ")
    assert result.extras["message_source"] == "meho"
    if status in (307, 308):
        assert "execution_outcome" not in result.extras
        return
    assert result.extras["execution_outcome"] == UNCERTAIN_OUTCOME
    assert result.extras["retry"] is False
    assert "remediation" in result.extras


_MARKERS = (
    "marker-sent-query",
    "marker-sent-fragment",
    "marker-sent-user",
    "marker-sent-pass",
    "marker-redirect-query",
    "marker-redirect-fragment",
    "marker-redirect-user",
    "marker-body",
)


def _marked_status_error(status: int, sent_path: str) -> httpx.HTTPStatusError:
    """A redirect with made-up marker values in every part that must stay hidden."""
    sent = httpx.URL(f"{_BASE}{sent_path}?t=marker-sent-query#marker-sent-fragment").copy_with(
        username="marker-sent-user", password="marker-sent-pass"
    )
    request = httpx.Request("PUT", sent)
    response = httpx.Response(
        status,
        headers={
            "Location": (
                "https://marker-redirect-user@svc.example.com/jobs/42"
                "?t=marker-redirect-query#marker-redirect-fragment"
            )
        },
        json={"message": "marker-body"},
        request=request,
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    return caught.value


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_switch_on_answer_hides_the_marked_values(status: int, answer_switch: Any) -> None:
    answer_switch(True)

    result = _classify(_marked_status_error(status, "/items/7"))

    dumped = json.dumps(result.model_dump(mode="json"))
    for marker in _MARKERS:
        assert marker not in dumped, marker
    assert f"{_BASE}/items/7" in dumped


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_the_switch_on_answer_hides_the_path_of_an_operation_that_sends_a_secret(
    status: int, answer_switch: Any
) -> None:
    answer_switch(True)

    result = _classify(
        _marked_status_error(status, "/keys/marker-secret-path"), op_sends_secret=True
    )

    dumped = json.dumps(result.model_dump(mode="json"))
    for marker in (*_MARKERS, "marker-secret-path", "/jobs/42"):
        assert marker not in dumped, marker
    assert _BASE in dumped
