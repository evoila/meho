# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared fixtures of the FreeIPA connector tests: a fake IPA server and helpers.

The fake server answers ``POST /ipa/session/login_password`` with session
cookies and ``POST /ipa/session/json`` per command, in FreeIPA's answer shape
(``result``, ``error``, ``id``, ``principal``, ``version``). Every JSON call is
recorded with its body and headers.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any
from uuid import UUID

import httpx
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.freeipa import (
    FREEIPA_CONNECTOR_ID,
    FREEIPA_PRODUCT,
    FREEIPA_VERSION,
    FreeipaConnector,
    register_freeipa_typed_operations,
)
from meho_backplane.operations import dispatch
from meho_backplane.operations._handler_resolve import get_or_create_connector_instance

HOST = "ipa.test.invalid"
BASE_URL = f"https://{HOST}"
LOGIN = "/ipa/session/login_password"
JSON = "/ipa/session/json"
USERNAME = "meho-svc"
PASSWORD = "made-up-ipa-password"
SESSION_COOKIE = "MagBearerToken=made-up-session"
PRINCIPAL = "meho-svc@EXAMPLE.TEST"

Answers = dict[str, Any]


class Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(
        self,
        *,
        version: str = "4.13.4",
        secret_ref: str = "targets/op-reads/freeipa",
        tls_server_name: str | None = None,
    ) -> None:
        self.product = FREEIPA_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = f"freeipa-{self.id.hex[:6]}"
        self.host = HOST
        self.port = 443
        self.secret_ref = secret_ref
        self.auth_model = "shared_service_account"
        self.tls_server_name = tls_server_name


def make_operator() -> Operator:
    return Operator(
        sub="op-reads-freeipa",
        name="FreeIPA Reads Operator",
        email=None,
        raw_jwt="op.reads.freeipa.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def credentials_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": USERNAME, "password": PASSWORD}


async def register() -> FreeipaConnector:
    """Store the operations and give the dispatcher's connector the test loader."""
    await register_freeipa_typed_operations()
    instance = get_or_create_connector_instance(FreeipaConnector)
    assert isinstance(instance, FreeipaConnector)
    instance._credentials_loader = credentials_loader
    return instance


def login_response(cookie: str = SESSION_COOKIE) -> httpx.Response:
    name, _, value = cookie.partition("=")
    return httpx.Response(
        200,
        headers=[("set-cookie", f"ipa_session={name}={value}; path=/ipa; httponly; secure")],
        text="",
    )


def ok_answer(result: Any) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": result,
            "error": None,
            "id": 0,
            "principal": PRINCIPAL,
            "version": "4.13.4",
        },
    )


def error_answer(name: str, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "result": None,
            "error": {"code": code, "message": message, "data": {}, "name": name},
            "id": 0,
            "principal": PRINCIPAL,
            "version": "4.13.4",
        },
    )


def fake_ipa(answers: Answers, calls: list[dict[str, Any]]) -> Callable[..., httpx.Response]:
    """A ``/ipa/session/json`` side effect: answer each command from *answers*."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"body": body, "headers": dict(request.headers)})
        answer = answers.get(body["method"])
        if answer is None:
            return error_answer("CommandError", 905, f"unknown command '{body['method']}'")
        if isinstance(answer, httpx.Response):
            return answer
        return ok_answer(answer)

    return _handler


PING = {"summary": "IPA server version 4.13.4. API version 2.257"}

HOST_ROW = {
    "dn": "fqdn=web01.example.test,cn=computers,cn=accounts,dc=example,dc=test",
    "fqdn": ["web01.example.test"],
    "description": ["Made-up web server"],
    "memberof_hostgroup": ["webservers"],
    "krblastpwdchange": [{"__datetime__": "20261001120000Z"}],
}

HBACTEST = {
    "summary": "Access granted: True",
    "value": True,
    "matched": ["allow_ssh_web"],
    "notmatched": ["allow_all"],
    "warning": None,
    "error": None,
}


def find_answer(rows: list[dict[str, Any]], *, truncated: bool = False) -> dict[str, Any]:
    return {
        "result": rows,
        "count": len(rows),
        "truncated": truncated,
        "summary": f"{len(rows)} matched",
    }


def show_answer(entry: dict[str, Any], value: str) -> dict[str, Any]:
    return {"result": entry, "value": value, "summary": None}


ALL_ANSWERS: Answers = {
    "ping": PING,
    "host_find": find_answer([HOST_ROW]),
    "host_show": show_answer(HOST_ROW, "web01.example.test"),
    "hostgroup_find": find_answer([{"cn": ["webservers"]}]),
    "hostgroup_show": show_answer(
        {"cn": ["webservers"], "member_host": ["web01.example.test"]}, "webservers"
    ),
    "user_find": find_answer([{"uid": ["jdoe"], "nsaccountlock": False}]),
    "user_show": show_answer({"uid": ["jdoe"], "memberof_group": ["ipausers"]}, "jdoe"),
    "group_find": find_answer([{"cn": ["admins"]}]),
    "group_show": show_answer({"cn": ["admins"], "member_user": ["admin"]}, "admins"),
    "role_find": find_answer([{"cn": ["Enrollment Administrator"]}]),
    "role_show": show_answer(
        {"cn": ["Enrollment Administrator"], "memberof_privilege": ["Host Enrollment"]},
        "Enrollment Administrator",
    ),
    "sudorule_find": find_answer([{"cn": ["admins-all"], "ipaenabledflag": [True]}]),
    "sudorule_show": show_answer({"cn": ["admins-all"]}, "admins-all"),
    "hbacrule_find": find_answer([{"cn": ["allow_all"], "ipaenabledflag": [True]}]),
    "hbacrule_show": show_answer({"cn": ["allow_all"]}, "allow_all"),
    "hbactest": HBACTEST,
    "pwpolicy_show": show_answer(
        {"cn": ["global_policy"], "krbmaxpwdlife": ["90"]}, "global_policy"
    ),
}


async def dispatch_op(op_id: str, params: dict[str, Any], target: Target | None = None) -> Any:
    return await dispatch(
        operator=make_operator(),
        connector_id=FREEIPA_CONNECTOR_ID,
        op_id=op_id,
        target=target or Target(),
        params=params,
    )


async def run(
    op_id: str, params: dict[str, Any], answers: Answers | None = None
) -> tuple[Any, list[dict[str, Any]], respx.Route]:
    """Dispatch one op against the fake server. Returns the result, the calls and the login."""
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=BASE_URL, assert_all_called=False) as mock:
        login = mock.post(LOGIN).mock(return_value=login_response())
        mock.post(JSON).mock(side_effect=fake_ipa(answers or ALL_ANSWERS, calls))
        result = await dispatch_op(op_id, params)
    return result, calls, login


__all__ = [
    "ALL_ANSWERS",
    "BASE_URL",
    "FREEIPA_VERSION",
    "HBACTEST",
    "HOST",
    "HOST_ROW",
    "JSON",
    "LOGIN",
    "PASSWORD",
    "PING",
    "PRINCIPAL",
    "SESSION_COOKIE",
    "USERNAME",
    "Answers",
    "Target",
    "credentials_loader",
    "dispatch_op",
    "error_answer",
    "fake_ipa",
    "find_answer",
    "login_response",
    "make_operator",
    "ok_answer",
    "register",
    "run",
    "show_answer",
]
