# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Salt RaaS connector: login, version, minions, keys, jobs, targets.

What is covered, with a respx-mocked RaaS server and no ingested catalog:

* the login (``GET /account/login`` with HTTP Basic -> cookies with
  ``_xsrf``) and, on every ``POST /rpc``, the Basic header, the cookies and
  ``X-Xsrftoken``;
* each of the six reads through ``dispatch()``: the RPC resource, method
  and kwarg sent, and the flat rows returned;
* an RPC ``error`` answer and an HTTP error reach the answer;
* a ``401`` on an RPC call logs in again for that target only and the read
  then succeeds;
* a job return is shown only when its content can be checked (a state run
  reduced to known fields, a number or a boolean), secret-named values are
  replaced, the arguments of functions outside a short list are withheld,
  a big return is left out with its size, and failed states are named;
* the stored trace of a call holds nothing a read withholds;
* no password, cookie or xsrf value in a result; registration shape;
  version parsing; fingerprint.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import (
    all_connectors_v2,
    clear_registry,
    register_connector_v2,
)
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.salt import (
    SALT_CONNECTOR_ID,
    SALT_IMPL_ID,
    SALT_PRODUCT,
    SALT_TYPED_OPS,
    SALT_VERSION,
    SaltConnector,
    register_salt_typed_operations,
)
from meho_backplane.connectors.salt.connector import parse_server_version, unwrap_rpc
from meho_backplane.connectors.salt.reads import JOIN_LIMIT, RETURN_BYTES_LIMIT
from meho_backplane.connectors.salt.typed_ops import JOB_STATES, KEY_STATES
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import DispatchTrace, DispatchTraceSpan, EndpointDescriptor, Tenant
from meho_backplane.flight_recorder.config import reset_flight_recorder_config_cache_for_testing
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings

_HOST = "salt-typed.test.invalid"
_BASE_URL = f"https://{_HOST}"
_LOGIN = "/account/login"
_RPC = "/rpc"
_PASSWORD = "typed-password-value"
_XSRF = "made-up-xsrf-value"
_SESSION_COOKIE = "made-up-session-cookie"

_VERSIONS = {
    "python": "3.10.0 (made up)",
    "opts": {"sql": {"dialect": "postgresql", "host": "<db-host>"}, "customer_id": "<id>"},
    "raas": {"raas": "8.18.0+made.up", "salt": "3006.0"},
}
_MASTERS = {
    "results": [
        {
            "uuid": "m-uuid-1",
            "master_id": "<master-1>",
            "cluster_id": None,
            "last_seen": "2026-01-01T00:00:00",
            "status": "present",
        }
    ],
    "count": 1,
}
_KEYS = {
    "results": [
        {"minion": "<minion-1>", "master": "<master-1>", "key_state": "accepted"},
        {"minion": "<minion-2>", "master": "<master-1>", "key_state": "pending"},
    ],
    "count": 2,
}
_PRESENCE = {
    "results": [
        {
            "minion": "<minion-1>",
            "master": "<master-1>",
            "status": "present",
            "timestamp": "2026-01-01T00:00:01",
        }
    ],
    "count": 1,
}
_CACHE = {
    "<master-1>": {
        "<minion-1>": {
            "grains": {
                "os": "Made-Up-OS",
                "osrelease": "1.0",
                "kernel": "Linux",
                "saltversion": "3006.0",
                "machine_id": "should-not-appear",
            }
        }
    }
}
_CMDS = {
    "count": 1,
    "results": [
        {
            "jid": "20260101000000000001",
            "cmd": "local",
            "fun": "state.apply",
            "state": "completed_failures",
            "user": "<user>",
            "start_time": "2026-01-01T00:00:00",
            "duration": 12.5,
            "tgt_name": "<target-name>",
            "tgt_uuid": "t-uuid-1",
            "job_name": None,
            "job_uuid": None,
            "sched_name": None,
            "origination": "Ad-Hoc",
            "is_highstate": False,
            "expected": 2,
            "returned": 2,
            "returned_good": 1,
            "returned_failed": 1,
            "user_uuid": "u-uuid-1",
        }
    ],
}
_STATE_RETURN = {
    "file_|-cfg_|-/etc/made-up.conf_|-managed": {
        "result": False,
        "name": "/etc/made-up.conf",
        "comment": "Source file not found",
        "changes": {},
    },
    "pkg_|-tool_|-tool_|-installed": {
        "result": True,
        "name": "tool",
        "comment": "ok",
        "changes": {"tool": {"old": "", "new": "1.0"}},
    },
}
_RETURNS = {
    "count": 2,
    "limit": 50,
    "results": [
        {
            "alter_time": "2026-01-01T00:00:05",
            "full_ret": {
                "id": "<minion-1>",
                "jid": "20260101000000000001",
                "fun": "state.apply",
                "fun_args": ["made-up-state"],
                "retcode": 2,
                "return": _STATE_RETURN,
                "success": False,
                "master_id": "<master-1>",
            },
            "fun": "state.apply",
            "fun_args": ["made-up-state"],
            "has_errors": True,
            "jid": "20260101000000000001",
            "master": "<master-1>",
            "minion_id": "<minion-1>",
            "return": _STATE_RETURN,
        },
        {
            "alter_time": "2026-01-01T00:00:06",
            "full_ret": {"retcode": 0, "success": True, "id": "<minion-2>"},
            "fun": "cmd.run",
            "fun_args": [],
            "has_errors": False,
            "jid": "20260101000000000001",
            "master": "<master-1>",
            "minion_id": "<minion-2>",
            "return": {"db_password": "plain-secret-value", "out": "done"},
        },
    ],
}
_TARGETS = {
    "count": 1,
    "limit": 50,
    "results": [
        {
            "uuid": "t-uuid-1",
            "name": "<target-name>",
            "desc": "made-up group",
            "tgt": {"*": {"tgt": "web*", "tgt_type": "glob"}},
            "pillars": [{"secret_pillar": "should-not-appear"}],
            "metadata": {"auth": {"owner": "<user>"}},
        }
    ],
}


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_module_state() -> Iterator[None]:
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()
    register_connector_v2(
        product=SALT_PRODUCT, version=SALT_VERSION, impl_id=SALT_IMPL_ID, cls=SaltConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
def _stub_embedding(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    monkeypatch.setattr(
        "meho_backplane.operations._search.get_embedding_service",
        lambda: service,
    )
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


class _Target:
    """Target satisfying the connector Protocols and the resolver shape."""

    def __init__(self, *, version: str = SALT_VERSION) -> None:
        self.product = SALT_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid.uuid4()
        self.tenant_id: UUID = uuid.UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = f"salt-typed-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/salt-typed"
        self.auth_model = "shared_service_account"


def _make_operator() -> Operator:
    return Operator(
        sub="op-reads-salt",
        name="Salt Reads Operator",
        email=None,
        raw_jwt="op.reads.salt.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _session_loader(_target: object, _operator: Operator) -> dict[str, str]:
    return {"username": "salt-typed-svc", "password": _PASSWORD}


async def _register(_stub_embedding: AsyncMock) -> SaltConnector:
    await register_salt_typed_operations()
    instance = get_or_create_connector_instance(SaltConnector)
    instance._session_loader = _session_loader  # type: ignore[attr-defined]
    assert isinstance(instance, SaltConnector)
    return instance


def _login_response(cookie: str = _SESSION_COOKIE) -> httpx.Response:
    return httpx.Response(
        200,
        headers=[
            ("set-cookie", f"_xsrf={_XSRF}; Path=/"),
            ("set-cookie", f"raas_session={cookie}; Path=/; HttpOnly"),
        ],
        json={"authenticated": True},
    )


_Answers = dict[tuple[str, str], Any]


def _fake_raas(answers: _Answers, calls: list[dict[str, Any]]) -> Callable[..., httpx.Response]:
    """An ``/rpc`` side effect: answer each (resource, method) from *answers*."""

    def _handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append({"body": body, "headers": dict(request.headers)})
        key = (body["resource"], body["method"])
        if key not in answers:
            return httpx.Response(
                200, json={"riq": 1, "ret": None, "error": {"message": "unknown"}, "warnings": []}
            )
        answer = answers[key]
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json={"riq": 1, "ret": answer, "error": None, "warnings": []})

    return _handler


_ALL_ANSWERS: _Answers = {
    ("api", "get_versions"): _VERSIONS,
    ("master", "get_master_presence"): _MASTERS,
    ("minions", "get_minion_key_state"): _KEYS,
    ("minions", "get_minion_presence"): _PRESENCE,
    ("minions", "get_minion_cache"): _CACHE,
    ("cmd", "get_cmds"): _CMDS,
    ("ret", "get_returns"): _RETURNS,
    ("tgt", "get_target_group"): _TARGETS,
}


async def _dispatch(op_id: str, params: dict[str, Any], target: _Target | None = None) -> Any:
    return await dispatch(
        operator=_make_operator(),
        connector_id=SALT_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params,
    )


async def _run(
    op_id: str, params: dict[str, Any], answers: _Answers | None = None
) -> tuple[Any, list[dict[str, Any]], respx.Route]:
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.get(_LOGIN).mock(return_value=_login_response())
        mock.post(_RPC).mock(side_effect=_fake_raas(answers or _ALL_ANSWERS, calls))
        result = await _dispatch(op_id, params)
    return result, calls, login


# ---------------------------------------------------------------------------
# Login and salt.about
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_logs_in_and_sends_basic_cookies_and_xsrf(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, login = await _run("salt.about", {})

    assert result.status == "ok", result.error
    assert login.call_count == 1
    assert login.calls[0].request.headers["authorization"].startswith("Basic ")
    assert [c["body"]["resource"] + "." + c["body"]["method"] for c in calls] == [
        "api.get_versions",
        "master.get_master_presence",
    ]
    headers = calls[0]["headers"]
    assert headers["authorization"].startswith("Basic ")
    assert headers["x-xsrftoken"] == _XSRF
    assert f"_xsrf={_XSRF}" in headers["cookie"]
    assert f"raas_session={_SESSION_COOKIE}" in headers["cookie"]
    assert calls[1]["body"]["kwarg"] == {"limit": 100}
    body = result.result
    assert body["raas_version"] == "8.18.0"
    assert body["raas_version_text"] == "8.18.0+made.up"
    assert body["components"] == [
        {"name": "raas", "version": "8.18.0+made.up"},
        {"name": "salt", "version": "3006.0"},
    ]
    assert body["master_count"] == 1
    assert body["masters"][0]["status"] == "present"
    assert "opts" not in json.dumps(body)
    assert "<db-host>" not in json.dumps(body)


@pytest.mark.asyncio
async def test_second_call_reuses_the_session(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    calls: list[dict[str, Any]] = []
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.get(_LOGIN).mock(return_value=_login_response())
        mock.post(_RPC).mock(side_effect=_fake_raas(_ALL_ANSWERS, calls))
        first = await _dispatch("salt.key.list", {}, target)
        second = await _dispatch("salt.key.list", {}, target)
    assert first.status == second.status == "ok"
    assert login.call_count == 1


@pytest.mark.asyncio
async def test_login_401_surfaces_connector_auth_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_LOGIN).respond(401)
        rpc = mock.post(_RPC).respond(200, json={"ret": {}})
        result = await _dispatch("salt.about", {})

    assert result.status == "error"
    assert rpc.call_count == 0
    assert "401" in (result.error or "") + json.dumps(result.extras, default=str)


@pytest.mark.asyncio
async def test_rpc_error_answer_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    error = httpx.Response(
        200,
        json={
            "riq": 2,
            "ret": None,
            "error": {"message": "made-up permission denied", "detail": {"state": "denied"}},
            "warnings": [],
        },
    )
    result, _, _ = await _run("salt.key.list", {}, {("minions", "get_minion_key_state"): error})
    assert result.status == "error"
    rendered = (result.error or "") + json.dumps(result.extras, default=str)
    assert "made-up permission denied" in rendered
    assert "minions.get_minion_key_state" in rendered


@pytest.mark.asyncio
async def test_http_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, _, _ = await _run(
        "salt.job.list", {}, {("cmd", "get_cmds"): httpx.Response(403, json={"message": "no"})}
    )
    assert result.status == "error"
    assert "403" in (result.error or "") + json.dumps(result.extras, default=str)


@pytest.mark.asyncio
async def test_401_on_rpc_logs_in_again_for_that_target_only(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    first, second = _Target(), _Target()
    calls: list[dict[str, Any]] = []
    rejected = {"done": False}
    ok = _fake_raas(_ALL_ANSWERS, calls)

    def _rpc(request: httpx.Request) -> httpx.Response:
        if "raas_session=old" in request.headers.get("cookie", "") and not rejected["done"]:
            rejected["done"] = True
            return httpx.Response(401)
        return ok(request)

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.get(_LOGIN).mock(
            side_effect=[_login_response("old"), _login_response("other"), _login_response("new")]
        )
        mock.post(_RPC).mock(side_effect=_rpc)
        assert (await _dispatch("salt.key.list", {}, second)).status == "ok"
        result = await _dispatch("salt.key.list", {}, first)
        again = await _dispatch("salt.key.list", {}, second)

    assert result.status == "ok", result.error
    assert again.status == "ok"
    assert login.call_count == 3
    cookies = [c["headers"]["cookie"] for c in calls]
    assert any("raas_session=new" in c for c in cookies)
    assert "raas_session=other" in cookies[-1]


# ---------------------------------------------------------------------------
# Minions and keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_minion_list_joins_key_state_presence_and_grains(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run("salt.minion.list", {"key_state": "accepted", "limit": 10})

    assert result.status == "ok", result.error
    sent = {(c["body"]["resource"], c["body"]["method"]): c["body"]["kwarg"] for c in calls}
    assert sent[("minions", "get_minion_key_state")] == {
        "key_state": "accepted",
        "sort_by": "minion_id",
        "limit": 10,
    }
    assert sent[("minions", "get_minion_presence")] == {"limit": JOIN_LIMIT}
    assert sent[("minions", "get_minion_cache")] == {"limit": JOIN_LIMIT}
    body = result.result
    assert body["count"] == 2
    assert body["details_complete"] is True
    assert body["minions"] == [
        {
            "minion_id": "<minion-1>",
            "master_id": "<master-1>",
            "key_state": "accepted",
            "presence": "present",
            "last_seen": "2026-01-01T00:00:01",
            "os": "Made-Up-OS",
            "os_release": "1.0",
            "kernel": "Linux",
            "salt_version": "3006.0",
        },
        {
            "minion_id": "<minion-2>",
            "master_id": "<master-1>",
            "key_state": "pending",
            "presence": None,
            "last_seen": None,
            "os": None,
            "os_release": None,
            "kernel": None,
            "salt_version": None,
        },
    ]
    assert "should-not-appear" not in json.dumps(body)


@pytest.mark.asyncio
async def test_minion_list_with_no_keys_makes_one_call(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    empty: _Answers = {("minions", "get_minion_key_state"): {"results": [], "count": 0}}
    result, calls, _ = await _run("salt.minion.list", {}, empty)
    assert result.status == "ok", result.error
    assert result.result == {"count": 0, "details_complete": True, "minions": []}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_minion_list_keeps_the_master_match_for_a_minion_id_under_two_masters(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A missing pair must not take the presence or OS of the other master."""
    await _register(_stub_embedding)
    answers = {
        **_ALL_ANSWERS,
        ("minions", "get_minion_key_state"): {
            "results": [
                {"minion": "<minion-1>", "master": "<master-1>", "key_state": "accepted"},
                {"minion": "<minion-1>", "master": "<master-2>", "key_state": "accepted"},
            ],
            "count": 2,
        },
    }
    result, _, _ = await _run("salt.minion.list", {}, answers)
    assert result.status == "ok", result.error
    first, second = result.result["minions"]
    assert (first["master_id"], first["presence"], first["os"]) == (
        "<master-1>",
        "present",
        "Made-Up-OS",
    )
    assert second["master_id"] == "<master-2>"
    assert (second["presence"], second["last_seen"], second["os"]) == (None, None, None)


@pytest.mark.asyncio
async def test_minion_list_reports_incomplete_details_on_a_big_estate(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    answers = {
        **_ALL_ANSWERS,
        ("minions", "get_minion_presence"): {**_PRESENCE, "count": JOIN_LIMIT + 1},
    }
    result, _, _ = await _run("salt.minion.list", {}, answers)
    assert result.status == "ok", result.error
    assert result.result["details_complete"] is False


@pytest.mark.asyncio
async def test_key_list_returns_flat_rows(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run(
        "salt.key.list", {"key_state": "pending", "master_id": "<master-1>", "page": 1}
    )
    assert result.status == "ok", result.error
    assert calls[0]["body"] == {
        "resource": "minions",
        "method": "get_minion_key_state",
        "kwarg": {
            "master_id": "<master-1>",
            "key_state": "pending",
            "sort_by": "minion_id",
            "limit": 100,
            "page": 1,
        },
    }
    assert result.result == {
        "count": 2,
        "keys": [
            {"minion_id": "<minion-1>", "master_id": "<master-1>", "key_state": "accepted"},
            {"minion_id": "<minion-2>", "master_id": "<master-1>", "key_state": "pending"},
        ],
    }


# ---------------------------------------------------------------------------
# Jobs and targets
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_list_sends_filters_and_returns_runs(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run(
        "salt.job.list", {"state": ["completed_failures"], "user": "<user>", "limit": 5}
    )
    assert result.status == "ok", result.error
    assert calls[0]["body"]["resource"] == "cmd"
    assert calls[0]["body"]["method"] == "get_cmds"
    assert calls[0]["body"]["kwarg"] == {
        "state": ["completed_failures"],
        "users": ["<user>"],
        "sort_by": "start_time",
        "reverse": True,
        "limit": 5,
    }
    body = result.result
    assert body["count"] == 1
    job = body["jobs"][0]
    assert job["jid"] == "20260101000000000001"
    assert job["state"] == "completed_failures"
    assert job["returned_failed"] == 1
    assert "user_uuid" not in job


@pytest.mark.asyncio
async def test_job_result_names_failed_states_and_replaces_secrets(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run(
        "salt.job.result", {"jid": "20260101000000000001", "has_errors": True}
    )
    assert result.status == "ok", result.error
    assert calls[0]["body"]["kwarg"] == {
        "jid": "20260101000000000001",
        "has_errors": True,
        "sort_by": "minion_id",
        "limit": 50,
    }
    body = result.result
    assert body["jid"] == "20260101000000000001"
    assert body["count"] == 2
    assert body["failed_minions"] == 1
    failed, good = body["results"]
    assert failed["minion_id"] == "<minion-1>"
    assert failed["success"] is False
    assert failed["retcode"] == 2
    assert failed["states_total"] == 2
    assert failed["states_failed"] == 1
    assert failed["failed_states"] == [
        _state_entry(state="file.managed", name="/etc/made-up.conf", result=False)
    ]
    assert failed["return_truncated"] is False
    assert failed["return_withheld"] is False
    assert failed["return"] == [
        _state_entry(state="file.managed", name="/etc/made-up.conf", result=False),
        _state_entry(state="pkg.installed", name="tool", result=True, changed=True),
    ]
    assert "Source file not found" not in json.dumps(body)
    assert failed["fun_args"] == ["made-up-state"]
    assert failed["fun_args_withheld"] is False
    assert good["return"] is None
    assert good["return_withheld"] is True
    assert good["fun_args"] is None
    assert good["fun_args_withheld"] is True
    assert good["states_total"] is None
    assert "plain-secret-value" not in json.dumps(body)


def _one_return(row: dict[str, Any]) -> _Answers:
    return {("ret", "get_returns"): {"count": 1, "results": [row]}}


def _state_entry(
    *, state: str | None, result: bool | None, name: str | None = None, changed: bool = False
) -> dict[str, Any]:
    """The reduced form of one state in a shown state run return."""
    return {
        "state": state,
        "name": name,
        "sls": None,
        "result": result,
        "changed": changed,
        "run_num": None,
        "start_time": None,
        "duration": None,
    }


@pytest.mark.asyncio
async def test_job_result_leaves_out_a_big_return_with_its_size(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    big = {
        f"cmd_|-step-{i}_|-echo {i}_|-run": {"result": True, "name": f"echo {i}", "comment": "ok"}
        for i in range(200)
    }
    answers = _one_return(
        {"minion_id": "<minion-1>", "jid": "1", "fun": "state.apply", "return": big}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["return"] is None
    assert row["return_truncated"] is True
    assert row["return_withheld"] is False
    assert row["return_bytes"] > RETURN_BYTES_LIMIT
    assert row["states_total"] == 200
    assert row["states_failed"] == 0


@pytest.mark.asyncio
async def test_job_result_withholds_a_pillar_get_string_return(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    answers = _one_return(
        {
            "minion_id": "<minion-1>",
            "jid": "1",
            "fun": "pillar.get",
            "fun_args": ["app:db:pw"],
            "return": "plain-pillar-value-1",
        }
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["return"] is None
    assert row["return_withheld"] is True
    assert row["return_truncated"] is False
    assert row["return_bytes"] == len(json.dumps("plain-pillar-value-1"))
    assert row["fun_args"] == ["app:db:pw"]
    assert row["fun_args_withheld"] is False
    assert row["states_total"] is None
    assert "plain-pillar-value-1" not in json.dumps(result.result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fun", "fun_args"),
    [
        ("user.setpassword", ["<user>", "plain-secret-value-6"]),
        ("cmd.run", ["mysql -u root -pplain-secret-value-7 -e 'select 1'"]),
        (None, ["plain-secret-value-8"]),
        ("grains.setval", ["api_token", "plain-secret-value-9"]),
        ("test.echo", ["plain-secret-value-10"]),
        ("pkg.install", ["tool", {"fromrepo": "plain-secret-value-11", "__kwarg__": True}]),
    ],
)
async def test_job_result_withholds_positional_arguments_of_other_functions(
    _stub_embedding: AsyncMock, session: AsyncSession, fun: str | None, fun_args: list[Any]
) -> None:
    """A positional argument has no name, so it can be a password: it is not shown.

    This holds also for a function of a module whose other functions take
    only names and keys (``grains.setval`` next to ``grains.get``).
    """
    await _register(_stub_embedding)
    answers = _one_return(
        {"minion_id": "<minion-1>", "jid": "1", "fun": fun, "fun_args": fun_args, "return": True}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun"] == fun
    assert row["fun_args"] is None
    assert row["fun_args_withheld"] is True
    assert row["return"] is True
    assert "plain-secret-value" not in json.dumps(result.result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fun", "fun_args"),
    [
        ("pkg.remove", ["tool"]),
        ("service.restart", ["made-up-svc"]),
        ("grains.get", ["os"]),
        ("test.ping", []),
    ],
)
async def test_job_result_keeps_the_arguments_of_name_only_functions(
    _stub_embedding: AsyncMock, session: AsyncSession, fun: str, fun_args: list[str]
) -> None:
    await _register(_stub_embedding)
    answers = _one_return(
        {"minion_id": "<minion-1>", "jid": "1", "fun": fun, "fun_args": fun_args, "return": True}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun_args"] == fun_args
    assert row["fun_args_withheld"] is False
    assert row["return"] is True
    assert row["return_withheld"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fun", "fun_args", "ret"),
    [
        ("file.read", ["/etc/made-up.secret"], "plain-secret-value-13"),
        ("cmd.run", ["cat /etc/made-up.secret"], "plain-secret-value-14\n"),
        ("grains.items", [], {"custom_grain": "plain-secret-value-15"}),
        ("cmd.run_all", [], ["plain-secret-value-16"]),
    ],
)
async def test_job_result_withholds_a_return_whose_content_cannot_be_checked(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    fun: str,
    fun_args: list[str],
    ret: Any,
) -> None:
    """A text, a list or a dict that is not a state run can hold a secret under no name."""
    await _register(_stub_embedding)
    answers = _one_return(
        {"minion_id": "<minion-1>", "jid": "1", "fun": fun, "fun_args": fun_args, "return": ret}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun"] == fun
    assert row["return"] is None
    assert row["return_withheld"] is True
    assert row["return_truncated"] is False
    assert row["return_bytes"] > 0
    assert row["states_total"] is None
    assert "plain-secret-value" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_job_result_withholds_nested_pillar_items(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    pillar = {"mysql": {"root_pw": "plain-pillar-value-2", "port": 3306}, "pw": "plain-pv-3"}
    answers = _one_return(
        {"full_ret": {"id": "<minion-1>", "jid": "1", "fun": "pillar.items", "return": pillar}}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun"] == "pillar.items"
    assert row["return"] is None
    assert row["return_withheld"] is True
    assert row["return_bytes"] > 0
    text = json.dumps(result.result)
    assert "plain-pillar-value-2" not in text
    assert "plain-pv-3" not in text


@pytest.mark.asyncio
async def test_job_result_hides_a_pillar_argument_of_a_state_run(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    state_return = {
        "cmd_|-hello_|-echo hi_|-run": {
            "result": True,
            "name": "echo hi",
            "changes": {"stdout": "plain-secret-value-12", "retcode": 0},
        }
    }
    answers = _one_return(
        {
            "minion_id": "<minion-1>",
            "jid": "1",
            "fun": "state.apply",
            "fun_args": [
                "web",
                {"pillar": {"site": {"pw": "plain-pillar-value-4"}}, "__kwarg__": True},
                "pillar={'pw': 'plain-pillar-value-5'}",
            ],
            "return": state_return,
        }
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun_args"] == [
        "web",
        {"pillar": "[REDACTED:param_name]", "__kwarg__": True},
        "pillar=[REDACTED:param_name]",
    ]
    assert row["return"] == [_state_entry(state="cmd.run", result=True, changed=True)]
    assert row["return_withheld"] is False
    assert row["states_total"] == 1
    text = json.dumps(result.result)
    assert "plain-pillar-value-4" not in text
    assert "plain-pillar-value-5" not in text
    assert "plain-secret-value-12" not in text
    assert "echo hi" not in text


@pytest.mark.asyncio
async def test_job_result_withholds_state_names_ids_and_comments(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The command of a ``cmd`` state is in the state key, its name and its comment.

    The answer and the stored trace keep only the checked fields of a
    state: kind, outcome, timing, and the name of a path or object state.
    The hidden values carry no secret-shaped word, so this test shows the
    reduction itself and not the shared scrubber.
    """
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    command = "mysql -u root -pplain-hidden-value-20 -e 'select 1'"
    state_return = {
        f"cmd_|-plain-hidden-value-21_|-{command}_|-run": {
            "__id__": "plain-hidden-value-21",
            "__sls__": "db.check",
            "__run_num__": 0,
            "name": command,
            "result": False,
            "comment": f'Command "{command}" run',
            "changes": {"stdout": "", "stderr": "plain-hidden-value-22", "retcode": 1},
            "start_time": "10:00:00.123456",
            "duration": 12.5,
        },
        "file_|-cfg_|-/etc/made-up.conf_|-managed": {
            "__id__": "cfg",
            "__sls__": "db.check",
            "__run_num__": 1,
            "name": "/etc/made-up.conf",
            "result": True,
            "comment": "File /etc/made-up.conf is in the correct state",
            "changes": {},
        },
        "cron_|-backup_|-/usr/bin/backup --with plain-hidden-value-23_|-present": {
            "__run_num__": 2,
            "name": "/usr/bin/backup --with plain-hidden-value-23",
            "result": True,
            "comment": "Cron /usr/bin/backup --with plain-hidden-value-23 already present",
            "changes": {},
        },
    }
    answers = _one_return(
        {
            "minion_id": "<minion-1>",
            "jid": "1",
            "fun": "state.apply",
            "fun_args": ["db.check"],
            "return": state_return,
        }
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["return_withheld"] is False
    assert row["states_total"] == 3
    assert row["states_failed"] == 1
    cmd_state = {
        "state": "cmd.run",
        "name": None,
        "sls": "db.check",
        "result": False,
        "changed": True,
        "run_num": 0,
        "start_time": "10:00:00.123456",
        "duration": 12.5,
    }
    assert row["failed_states"] == [cmd_state]
    assert row["return"] == [
        cmd_state,
        {
            **_state_entry(state="file.managed", name="/etc/made-up.conf", result=True),
            "sls": "db.check",
            "run_num": 1,
        },
        {**_state_entry(state="cron.present", result=True), "run_num": 2},
    ]
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == 1
    recorded = spans[0].attributes["response_body"]["ret"]["results"][0]
    assert recorded["failed_states"] == [cmd_state]
    assert recorded["return"] == row["return"]
    for text in (json.dumps(result.result), _render(spans)):
        assert "plain-hidden-value" not in text
        assert "mysql" not in text
        assert "/usr/bin/backup" not in text


@pytest.mark.asyncio
async def test_job_result_counts_failed_states_whose_id_names_a_password(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A state id such as ``reset_password`` hides neither that state nor the others.

    The shared scrubber reads such an id as a secret field name. The answer
    and the stored trace still count both failed states and name them by
    module and function; the id, the command and the changes stay out.
    """
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    command = "reset-tool --user app --value plain-hidden-value-31"
    state_return = {
        f"cmd_|-reset_password_|-{command}_|-run": {
            "__id__": "reset_password",
            "__run_num__": 0,
            "name": command,
            "result": False,
            "comment": f'Command "{command}" run',
            "changes": {"password": "plain-hidden-value-32"},
        },
        "service_|-app_|-app_|-running": {
            "__id__": "app",
            "__run_num__": 1,
            "name": "app",
            "result": False,
            "comment": "Service app is not running",
            "changes": {},
        },
        "file_|-cfg_|-/etc/made-up.conf_|-managed": {
            "__run_num__": 2,
            "name": "/etc/made-up.conf",
            "result": True,
            "comment": "ok",
            "changes": {},
        },
    }
    answers = _one_return(
        {
            "minion_id": "<minion-1>",
            "jid": "1",
            "fun": "state.apply",
            "fun_args": ["app"],
            "return": state_return,
        }
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["return_withheld"] is False
    assert row["states_total"] == 3
    assert row["states_failed"] == 2
    failed = [
        {**_state_entry(state="cmd.run", result=False, changed=True), "run_num": 0},
        {**_state_entry(state="service.running", name="app", result=False), "run_num": 1},
    ]
    assert row["failed_states"] == failed
    assert row["return"] == [
        *failed,
        {
            **_state_entry(state="file.managed", name="/etc/made-up.conf", result=True),
            "run_num": 2,
        },
    ]
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == 1
    recorded = spans[0].attributes["response_body"]["ret"]["results"][0]
    assert recorded["states_total"] == 3
    assert recorded["states_failed"] == 2
    assert recorded["failed_states"] == failed
    assert recorded["return"] == row["return"]
    for text in (json.dumps(result.result), _render(spans)):
        assert "plain-hidden-value" not in text
        assert "reset_password" not in text
        assert "reset-tool" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("fun", ["pillar.get", "grains.get"])
async def test_job_result_withholds_the_default_value_of_a_key_lookup(
    _stub_embedding: AsyncMock, session: AsyncSession, fun: str
) -> None:
    """The second argument of ``pillar.get`` is a default value, which can be a password."""
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    fun_args = [
        "app:db:pw",
        "plain-secret-value-24",
        {"default": "plain-secret-value-25", "delimiter": ":", "__kwarg__": True},
        "default=plain-secret-value-26",
    ]
    answers = _one_return(
        {"minion_id": "<minion-1>", "jid": "1", "fun": fun, "fun_args": fun_args, "return": 5}
    )
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    row = result.result["results"][0]
    assert row["fun_args"] == [
        "app:db:pw",
        "[REDACTED:param_name]",
        {"default": "[REDACTED:param_name]", "delimiter": ":", "__kwarg__": True},
        "[REDACTED:param_name]",
    ]
    assert row["fun_args_withheld"] is False
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == 1
    recorded = spans[0].attributes["response_body"]["ret"]["results"][0]
    assert recorded["fun_args"] == row["fun_args"]
    for text in (json.dumps(result.result), _render(spans)):
        assert "plain-secret-value" not in text


@pytest.mark.asyncio
async def test_job_result_needs_a_jid(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run("salt.job.result", {})
    assert result.status == "error"
    assert calls == []


@pytest.mark.asyncio
async def test_target_list_reads_no_pillar_data(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    result, calls, _ = await _run("salt.target.list", {"name": "<target-name>"})
    assert result.status == "ok", result.error
    assert calls[0]["body"]["kwarg"] == {
        "name": "<target-name>",
        "include_pillar_data": False,
        "limit": 50,
    }
    assert result.result == {
        "count": 1,
        "targets": [
            {
                "uuid": "t-uuid-1",
                "name": "<target-name>",
                "desc": "made-up group",
                "targets": [{"master_id": "*", "tgt_type": "glob", "tgt": "web*"}],
            }
        ],
    }
    assert "should-not-appear" not in json.dumps(result.result)


# ---------------------------------------------------------------------------
# Secrets, parsing, registration, fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("salt.about", {}),
        ("salt.minion.list", {}),
        ("salt.key.list", {}),
        ("salt.job.list", {}),
        ("salt.job.result", {"jid": "20260101000000000001"}),
        ("salt.target.list", {}),
    ],
)
async def test_results_hold_no_password_cookie_or_xsrf(
    _stub_embedding: AsyncMock, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    await _register(_stub_embedding)
    result, _, _ = await _run(op_id, params)
    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _XSRF, _SESSION_COOKIE):
        assert secret not in rendered


async def _enable_trace_capture(session: AsyncSession) -> None:
    """Seed the operator's tenant with the flight recorder on."""
    session.add(
        Tenant(
            id=_make_operator().tenant_id,
            slug="salt-reads",
            name="salt-reads",
            flight_recorder_enabled=True,
        )
    )
    await session.commit()
    reset_flight_recorder_config_cache_for_testing()


async def _vendor_call_spans(audit_id: UUID) -> list[DispatchTraceSpan]:
    async with get_sessionmaker()() as s:
        trace = (
            await s.execute(select(DispatchTrace).where(DispatchTrace.audit_id == audit_id))
        ).scalar_one()
        spans = (
            await s.execute(
                select(DispatchTraceSpan)
                .where(DispatchTraceSpan.trace_id == trace.id)
                .order_by(DispatchTraceSpan.seq.asc())
            )
        ).scalars()
        return [span for span in spans if span.span_kind == "vendor_call"]


def _render(spans: list[DispatchTraceSpan]) -> str:
    return json.dumps([f"{span.name}|{span.attributes}" for span in spans], default=str)


@pytest.mark.asyncio
async def test_job_result_trace_holds_only_what_the_answer_shows(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """The recorded vendor answer passes the same rules as the read: no withheld value in it."""
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    answers: _Answers = {
        ("ret", "get_returns"): {
            "count": 3,
            "results": [
                {
                    "minion_id": "<minion-1>",
                    "jid": "1",
                    "fun": "pillar.get",
                    "fun_args": ["app:db:pw"],
                    "return": "plain-pillar-value-1",
                },
                {
                    "minion_id": "<minion-2>",
                    "jid": "1",
                    "fun": "file.read",
                    "fun_args": ["/etc/made-up.secret"],
                    "return": "plain-secret-value-13",
                },
                {
                    "minion_id": "<minion-3>",
                    "jid": "1",
                    "fun": "grains.setval",
                    "fun_args": ["api_token", "plain-secret-value-9"],
                    "return": {"api_token": "plain-secret-value-9"},
                },
            ],
        }
    }
    result, _, _ = await _run("salt.job.result", {"jid": "1"}, answers)
    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == 1
    span = spans[0]
    assert span.name == f"POST {_BASE_URL}{_RPC}"
    assert span.attributes["body_recorded"] is True
    recorded = span.attributes["response_body"]
    assert recorded["error"] is None
    assert recorded["ret"]["count"] == 3
    rows = recorded["ret"]["results"]
    assert [row["fun"] for row in rows] == ["pillar.get", "file.read", "grains.setval"]
    assert [row["return"] for row in rows] == [None, None, None]
    assert [row["return_withheld"] for row in rows] == [True, True, True]
    assert rows[0]["fun_args"] == ["app:db:pw"]
    assert rows[2]["fun_args"] is None
    assert rows[2]["fun_args_withheld"] is True
    rendered = _render(spans)
    for secret in ("plain-pillar-value-1", "plain-secret-value", _PASSWORD, _XSRF, _SESSION_COOKIE):
        assert secret not in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params"),
    [
        ("salt.about", {}),
        ("salt.minion.list", {}),
        ("salt.key.list", {}),
        ("salt.job.list", {}),
        ("salt.job.result", {"jid": "20260101000000000001"}),
        ("salt.target.list", {}),
    ],
)
async def test_traces_hold_no_withheld_value(
    _stub_embedding: AsyncMock, session: AsyncSession, op_id: str, params: dict[str, Any]
) -> None:
    """Server settings, pillar data and secret-named values stay out of every stored trace."""
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    result, calls, _ = await _run(op_id, params)
    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == len(calls)
    assert all(span.attributes["body_recorded"] is True for span in spans)
    rendered = _render(spans)
    for secret in (
        _PASSWORD,
        _XSRF,
        _SESSION_COOKIE,
        "<db-host>",
        "secret_pillar",
        "plain-secret",
        "should-not-appear",
    ):
        assert secret not in rendered


#: A value under an ordinary field name that no read shows. The shared
#: redaction cannot know it is a secret, so only the field allowlist of
#: the trace keeps it out.
_EXTRA = "plain-extra-value"


def _with_extra(answer: dict[str, Any], number: int) -> dict[str, Any]:
    """A paged answer whose rows carry one more field, and one more top-level key."""
    rows = [{**row, "notes": f"{_EXTRA}-{number}"} for row in answer["results"]]
    return {**answer, "results": rows, "summary": f"{_EXTRA}-{number}"}


_EXTRA_ANSWERS: _Answers = {
    ("api", "get_versions"): {**_VERSIONS, "build_env": f"{_EXTRA}-1"},
    ("master", "get_master_presence"): _with_extra(_MASTERS, 2),
    ("minions", "get_minion_key_state"): _with_extra(_KEYS, 3),
    ("minions", "get_minion_presence"): _with_extra(_PRESENCE, 4),
    ("minions", "get_minion_cache"): {
        "<master-1>": {
            "<minion-1>": {
                "grains": {
                    **_CACHE["<master-1>"]["<minion-1>"]["grains"],
                    "app_conf": f"{_EXTRA}-5",
                },
                "pillar": {"app": f"{_EXTRA}-6"},
            }
        }
    },
    ("cmd", "get_cmds"): {
        **_with_extra(_CMDS, 7),
        "results": [{**_CMDS["results"][0], "arg": ["<user>", f"{_EXTRA}-7"]}],
    },
    ("ret", "get_returns"): _RETURNS,
    ("tgt", "get_target_group"): {
        **_with_extra(_TARGETS, 8),
        "results": [
            {
                **_TARGETS["results"][0],
                "tgt": {"*": {"tgt": "web*", "tgt_type": "glob", "notes": f"{_EXTRA}-8"}},
            }
        ],
    },
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "params", "kept"),
    [
        ("salt.about", {}, ("8.18.0+made.up", "<master-1>")),
        ("salt.minion.list", {}, ("Made-Up-OS", "<minion-1>", "accepted")),
        ("salt.key.list", {}, ("<minion-2>", "pending")),
        ("salt.job.list", {}, ("20260101000000000001", "completed_failures")),
        ("salt.target.list", {}, ("t-uuid-1", "web*")),
    ],
)
async def test_traces_keep_only_the_fields_the_read_uses(
    _stub_embedding: AsyncMock,
    session: AsyncSession,
    op_id: str,
    params: dict[str, Any],
    kept: tuple[str, ...],
) -> None:
    """A custom grain, a cache pillar or any other unread field stays out of the stored trace."""
    await _register(_stub_embedding)
    await _enable_trace_capture(session)
    result, calls, _ = await _run(op_id, params, _EXTRA_ANSWERS)
    assert result.status == "ok", result.error
    assert result.audit_id is not None
    spans = await _vendor_call_spans(result.audit_id)
    assert len(spans) == len(calls)
    assert all(span.attributes["body_recorded"] is True for span in spans)
    rendered = _render(spans)
    assert _EXTRA not in rendered
    for value in kept:
        assert value in rendered


@pytest.mark.parametrize(
    ("ret", "version", "raw"),
    [
        (_VERSIONS, "8.18.0", "8.18.0+made.up"),
        ({"raas": "9.1.0.0-123"}, "9.1.0.0", "9.1.0.0-123"),
        ({"raas": {"raas": "v-made-up"}}, None, "v-made-up"),
        ({}, None, None),
        ([], None, None),
    ],
)
def test_parse_server_version_shapes(ret: Any, version: str | None, raw: str | None) -> None:
    read = parse_server_version(ret)
    assert (read.version, read.raw) == (version, raw)
    assert (read.error is None) == (version is not None)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "not a JSON object"),
        ({"riq": 1}, "has no ret"),
        ({"error": "plain text error"}, "plain text error"),
        ({"error": {"message": "m", "detail": "d" * 900}}, "m ("),
    ],
)
def test_unwrap_rpc_errors(payload: Any, message: str) -> None:
    with pytest.raises(RuntimeError, match=r"salt rpc api\.get_versions") as info:
        unwrap_rpc(payload, ("api", "get_versions"))
    assert message in str(info.value)
    assert len(str(info.value)) < 1200


@pytest.mark.asyncio
async def test_registered_ops_are_typed_safe_and_documented(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_salt_typed_operations()
    rows = (
        (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == SALT_PRODUCT,
                    EndpointDescriptor.impl_id == SALT_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.op_id for r in rows} == {op.op_id for op in SALT_TYPED_OPS}
    assert {r.op_id for r in rows} == {
        "salt.about",
        "salt.minion.list",
        "salt.key.list",
        "salt.job.list",
        "salt.job.result",
        "salt.target.list",
    }
    for row in rows:
        assert row.source_kind == "typed"
        assert row.handler_ref is not None
        assert row.safety_level == "safe"
        assert row.requires_approval is False
        assert "read-only" in row.tags
        assert row.parameter_schema["additionalProperties"] is False
        assert len(row.parameter_schema["examples"]) == 1
        assert {"when_to_call", "output_shape", "next_step"} <= set(row.llm_instructions)
        assert "\n" not in row.summary and len(row.description) <= 300
    by_id = {row.op_id: row for row in rows}
    assert by_id["salt.key.list"].parameter_schema["properties"]["key_state"]["enum"] == list(
        KEY_STATES
    )
    job_state = by_id["salt.job.list"].parameter_schema["properties"]["state"]
    assert job_state["items"]["enum"] == list(JOB_STATES)


def test_package_registers_versioned_connector_and_wildcard() -> None:
    clear_registry()
    register_connector_v2(
        product=SALT_PRODUCT, version=SALT_VERSION, impl_id=SALT_IMPL_ID, cls=SaltConnector
    )
    register_connector_v2(product=SALT_PRODUCT, version="", impl_id="", cls=SaltConnector)
    registry = all_connectors_v2()
    assert registry[(SALT_PRODUCT, SALT_VERSION, SALT_IMPL_ID)] is SaltConnector
    assert registry[(SALT_PRODUCT, "", "")] is SaltConnector
    assert SaltConnector.supported_version_range == ">=8.0,<10.0"
    assert SALT_CONNECTOR_ID == "salt-raas-8.18"


@pytest.mark.parametrize("version", ["8.18", "8.18.0", "9.1", "9.1.0", "9.1.1"])
def test_each_covered_version_resolves_to_the_connector(version: str) -> None:
    assert resolve_connector(_Target(version=version)) is SaltConnector


@pytest.mark.asyncio
async def test_fingerprint_reports_version_and_unreachable_shape() -> None:
    connector = SaltConnector(session_loader=_session_loader)
    calls: list[dict[str, Any]] = []
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_LOGIN).mock(return_value=_login_response())
        mock.post(_RPC).mock(side_effect=_fake_raas(_ALL_ANSWERS, calls))
        fp = await connector.fingerprint(_Target(), _make_operator())
    assert (fp.vendor, fp.product, fp.reachable) == ("vmware", "salt", True)
    assert (fp.version, fp.build) == ("8.18.0", "8.18.0+made.up")
    await connector.aclose()

    connector = SaltConnector(session_loader=_session_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_LOGIN).respond(401)
        fp = await connector.fingerprint(_Target(), _make_operator())
        probe = await connector.probe(_Target())
    assert fp.reachable is False
    assert "401" in str(fp.extras["error"])
    assert probe.ok is False
    await connector.aclose()


@pytest.mark.asyncio
async def test_401_on_the_version_read_drops_the_session() -> None:
    connector = SaltConnector(session_loader=_session_loader)
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.get(_LOGIN).mock(return_value=_login_response())
        mock.post(_RPC).respond(401)
        first = await connector.server_version(target, _make_operator())
        second = await connector.server_version(target, _make_operator())
    assert first.error is not None and second.error is not None
    assert login.call_count == 2
    await connector.aclose()


@pytest.mark.asyncio
async def test_auth_headers_rejects_other_auth_models() -> None:
    connector = SaltConnector(session_loader=_session_loader)
    target = _Target()
    target.auth_model = "per_user"
    with pytest.raises(NotImplementedError, match="per_user"):
        await connector.auth_headers(target, _make_operator())
