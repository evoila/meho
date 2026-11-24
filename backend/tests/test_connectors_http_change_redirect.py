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
from meho_backplane.connectors.adapters.http import (
    HttpConnector,
    _SameOriginRedirectClient,
    change_sent_once_before,
)
from meho_backplane.connectors.keycloak import KeycloakConnector
from meho_backplane.connectors.keycloak.session import (
    KeycloakAdminCredentials,
    KeycloakClientCredentials,
    KeycloakTargetLike,
)
from meho_backplane.flight_recorder import capture
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
async def test_log_line_of_a_change_redirect_keeps_only_the_origin(location: str) -> None:
    # The sent path and the path the target names can both carry a secret
    # value of the call. The log line of a change names the origins only.
    server = _Server(_redirect(307, location))
    with capture_logs() as captured:
        async with _SameOriginRedirectClient(
            base_url=_BASE, transport=httpx.MockTransport(server)
        ) as client:
            await client.request("PUT", "/keys/sent-secret-value", json={"name": "widget"})

    warnings = [e for e in captured if e["event"].startswith("connector_redirect_not_followed")]
    assert len(warnings) == 1
    dumped = json.dumps(captured, default=str)
    assert "sent-secret-value" not in dumped
    assert "redirect-secret-value" not in dumped
    assert warnings[0]["from_url"] == _BASE


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
async def test_a_login_during_a_repeat_keeps_its_usual_redirect_rule() -> None:
    # A fresh login before the repeat is a direct client call. Its 307 to its
    # own path with a trailing slash is followed, as it is outside a repeat.
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/session":
            return httpx.Response(307, headers={"Location": "/api/session/"})
        return httpx.Response(200, json={"token": "made-up"})

    server = _Server(answer)
    with change_sent_once_before():
        resp = await _send(server, "POST", "/api/session")
    assert resp.status_code == 200
    assert server.seen == [("POST", "/api/session"), ("POST", "/api/session/")]


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


def _blocked_after_first_send() -> tuple[list[str], Any]:
    """A block check that lets the first send go and refuses every later one."""
    checks: list[str] = []

    async def check(operator: Operator) -> None:
        checks.append(operator.sub)
        if len(checks) > 1:
            raise PrincipalBlockedStepError(unavailable=False)

    return checks, patch(
        "meho_backplane.connectors.adapters.http.raise_if_step_login_blocked", side_effect=check
    )


@pytest.mark.asyncio
async def test_login_blocked_before_a_followed_change_redirect_gets_no_second_request() -> None:
    checks, blocked = _blocked_after_first_send()
    conn = _Conn()
    with blocked:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            first = mock.put("/items/7").respond(307, headers={"Location": "/items/7/"})
            second = mock.put("/items/7/").respond(200, json={"ok": True})
            with pytest.raises(PrincipalBlockedStepError):
                await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert checks == ["test-operator", "test-operator"]
    assert first.call_count == 1
    assert second.call_count == 0


@pytest.mark.asyncio
async def test_login_blocked_before_a_followed_read_redirect_gets_no_second_request() -> None:
    checks, blocked = _blocked_after_first_send()
    conn = _Conn()
    with blocked:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            first = mock.get("/list").respond(307, headers={"Location": "/list/"})
            second = mock.get("/list/").respond(200, json={"items": []})
            with pytest.raises(PrincipalBlockedStepError):
                await conn._request_json(_target(), "GET", "/list", operator=_operator())
    await conn.aclose()

    assert len(checks) == 2
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


def _real_capture(op_id: str, tags: tuple[str, ...] = ()) -> Any:
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


@pytest.mark.asyncio
async def test_trace_of_an_operation_that_sends_a_secret_keeps_only_the_origin() -> None:
    conn = _Conn()
    with _real_capture("PUT:/keys/{name}", tags=("secret",)) as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put("/keys/sent-secret-value").respond(
                307, headers={"Location": "/keys/sent-secret-value/"}
            )
            mock.put("/keys/sent-secret-value/").respond(200, json={"ok": True})
            await conn._post_json(
                _target(), "/keys/sent-secret-value", operator=_operator(), verb="PUT"
            )
    await conn.aclose()

    assert [(span.name, span.status) for span in spans] == [
        (f"PUT {_BASE}", "307"),
        (f"PUT {_BASE}", "200"),
    ]
    assert [span.attributes["url"] for span in spans] == [_BASE, _BASE]
    assert "sent-secret-value" not in _dumped(spans)


@pytest.mark.asyncio
async def test_trace_of_an_operation_that_sends_a_secret_keeps_only_the_origin_on_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    conn = _Conn()
    with _real_capture("PUT:/keys/{name}", tags=("secret",)) as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put("/keys/sent-secret-value").mock(side_effect=fail)
            with (
                patch("asyncio.sleep", new=AsyncMock()),
                pytest.raises(httpx.ConnectError),
            ):
                await conn._post_json(
                    _target(), "/keys/sent-secret-value", operator=_operator(), verb="PUT"
                )
    await conn.aclose()

    assert spans
    assert {span.attributes["url"] for span in spans} == {_BASE}
    assert "sent-secret-value" not in _dumped(spans)


@pytest.mark.asyncio
async def test_trace_of_another_operation_keeps_the_path() -> None:
    conn = _Conn()
    with _real_capture("PUT:/items/{id}") as spans:
        async with respx.mock(base_url=_BASE) as mock:
            mock.put("/items/7").respond(307, headers={"Location": "/items/7/"})
            mock.put("/items/7/").respond(200, json={"ok": True})
            await conn._post_json(_target(), "/items/7", operator=_operator(), verb="PUT")
    await conn.aclose()

    assert [span.attributes["url"] for span in spans] == [f"{_BASE}/items/7", f"{_BASE}/items/7/"]


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
