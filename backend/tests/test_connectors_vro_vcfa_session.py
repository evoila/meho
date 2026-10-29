# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Orchestrator connector's VCF 9 login and the reads a controlled run needs.

What is covered, with mocked VCF Automation and Orchestrator answers:

* ``extras.login: vcfa-session``: the login, a read with the session
  token, reuse of the token, a new login 60 seconds before the token ends
  and after a ``401``; a wrong password or a missing org gives
  ``connector_auth_failed`` with the fix; the password goes only to the
  target's host; no token in any result, log line, audit row or trace;
* ``vro.workflow.get``: ``content_sha256`` and ``secret`` on inputs;
* ``vro.execution.get``: ``outcome`` for every vendor run state, ``unknown``
  for any other state and ``not_reachable`` when the server does not answer;
* ``more_rows`` on every list read.

All names and values are made up.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import respx
import structlog
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_shared import vcfa_session
from meho_backplane.connectors.vro import (
    VRO_CONNECTOR_ID,
    VRO_IMPL_ID,
    VRO_PRODUCT,
    VRO_VERSION,
    VroConnector,
    register_vro_typed_operations,
)
from meho_backplane.connectors.vro import connector as vro_connector_module
from meho_backplane.connectors.vro.typed_reads import OUTCOME_BY_STATE
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.flight_recorder import capture
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.settings import get_settings
from tests.test_connectors_vcfa_session_shared import make_token

_HOST = "vro-vcfa.test.invalid"
_BASE_URL = f"https://{_HOST}"
_PASSWORD = "made-up-vro-vcfa-password"
_ORG_LOGIN = "/cloudapi/1.0.0/sessions"
_TOKEN_HEADER = "X-VMWARE-VCLOUD-ACCESS-TOKEN"
_WF = "wf-0001"
_RUN = "run-0001"
_ABOUT = "/vco/api/about"
_WORKFLOWS = "/vco/api/workflows"
_RUNS = f"/vco/api/workflows/{_WF}/executions"
_RUN_PATH = f"{_RUNS}/{_RUN}"
_ABOUT_PAYLOAD = {"version": "9.1.0", "build-number": "1"}


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
        product=VRO_PRODUCT, version=VRO_VERSION, impl_id=VRO_IMPL_ID, cls=VroConnector
    )
    yield
    reset_dispatcher_caches()
    reset_handler_cache()
    clear_registry()


@pytest.fixture
async def connector(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[VroConnector]:
    monkeypatch.setattr(
        "meho_backplane.operations.typed_register.encode_endpoint_text",
        AsyncMock(return_value=[0.1] * 384),
    )
    await register_vro_typed_operations()
    instance = get_or_create_connector_instance(VroConnector)
    assert isinstance(instance, VroConnector)
    instance._credentials_loader = AsyncMock(
        return_value={"username": "svc-made-up", "password": _PASSWORD}
    )
    async with get_sessionmaker()():
        yield instance


class _Target:
    """Target satisfying ``VroTargetLike`` and the resolver shape."""

    def __init__(self, *, login: str | None = "vcfa-session", org: str | None = "made-up-org"):
        self.product = VRO_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "9.1.0"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000e0")
        self.name = f"vro-vcfa-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/made-up/vro-vcfa"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}
        if login is not None:
            self.extras["login"] = login
        if org is not None:
            self.extras["org"] = org


def _operator() -> Operator:
    return Operator(
        sub="op-vro-vcfa",
        name="Orchestrator VCF Operator",
        email=None,
        raw_jwt="op.vro.vcfa.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000e4"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch(op_id: str, params: dict[str, Any] | None = None, target: Any = None) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=VRO_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params or {},
    )


def _session_answer(token: str) -> httpx.Response:
    return httpx.Response(200, headers={_TOKEN_HEADER: token}, json={"id": "made-up-session"})


def _link(**attributes: Any) -> dict[str, Any]:
    return {
        "href": f"https://<orchestrator>/vco/api/x/{attributes.get('id')}/",
        "attributes": [{"name": key, "value": value} for key, value in attributes.items()],
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vcfa_session_logs_in_reads_and_reuses_the_token(connector: VroConnector) -> None:
    token = make_token({"exp": time.time() + 3600})
    target = _Target()
    async with respx.mock(assert_all_called=False) as mock:
        login = mock.post(f"{_BASE_URL}{_ORG_LOGIN}").mock(return_value=_session_answer(token))
        about = mock.get(f"{_BASE_URL}{_ABOUT}").respond(200, json=_ABOUT_PAYLOAD)
        first = await _dispatch("vro.about", target=target)
        second = await _dispatch("vro.about", target=target)

    assert first.status == "ok", first.error
    assert second.status == "ok", second.error
    assert login.call_count == 1
    assert login.calls[0].request.url.host == _HOST
    assert [call.request.headers["authorization"] for call in about.calls] == [
        f"Bearer {token}",
        f"Bearer {token}",
    ]
    assert first.result["version"] == "9.1.0"


@pytest.mark.asyncio
async def test_vcfa_session_logs_in_again_before_the_token_ends(connector: VroConnector) -> None:
    short = make_token({"exp": time.time() + 30, "n": 1})
    fresh = make_token({"exp": time.time() + 3600, "n": 2})
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_ORG_LOGIN)
        login.side_effect = [_session_answer(short), _session_answer(fresh)]
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        assert (await _dispatch("vro.about", target=target)).status == "ok"
        assert (await _dispatch("vro.about", target=target)).status == "ok"
        assert (await _dispatch("vro.about", target=target)).status == "ok"

    assert login.call_count == 2
    assert [call.request.headers["authorization"] for call in about.calls] == [
        f"Bearer {short}",
        f"Bearer {fresh}",
        f"Bearer {fresh}",
    ]


@pytest.mark.asyncio
async def test_vcfa_session_logs_in_again_after_a_401(connector: VroConnector) -> None:
    first = make_token({"exp": time.time() + 3600, "n": 1})
    second = make_token({"exp": time.time() + 3600, "n": 2})
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_ORG_LOGIN)
        login.side_effect = [_session_answer(first), _session_answer(second)]
        about = mock.get(_ABOUT)
        about.side_effect = [httpx.Response(401), httpx.Response(200, json=_ABOUT_PAYLOAD)]
        result = await _dispatch("vro.about")

    assert result.status == "ok", result.error
    assert login.call_count == 2
    assert about.calls[1].request.headers["authorization"] == f"Bearer {second}"


@pytest.mark.asyncio
async def test_targets_share_a_login_only_when_the_org_is_the_same(
    connector: VroConnector,
) -> None:
    """One secret and one host: the same org shares one login, another org logs in alone."""
    token_a = make_token({"exp": time.time() + 3600, "n": 1})
    token_b = make_token({"exp": time.time() + 3600, "n": 2})
    first, second = _Target(org="org-a"), _Target(org="org-a")
    other = _Target(org="org-b")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        login = mock.post(_ORG_LOGIN)
        login.side_effect = [_session_answer(token_a), _session_answer(token_b)]
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        for target in (first, second, other):
            assert (await _dispatch("vro.about", target=target)).status == "ok"

    assert login.call_count == 2
    assert [call.request.headers["authorization"] for call in about.calls] == [
        f"Bearer {token_a}",
        f"Bearer {token_a}",
        f"Bearer {token_b}",
    ]


@pytest.mark.asyncio
async def test_wrong_password_gives_connector_auth_failed_with_the_fix(
    connector: VroConnector,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ORG_LOGIN).respond(401)
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        result = await _dispatch("vro.about")

    assert result.status == "error"
    assert about.call_count == 0
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["cause"] == "session_establish_401"
    assert "made-up-org" in result.extras["remediation"]
    assert "current password" in result.extras["remediation"]
    assert _PASSWORD not in json.dumps(result.extras, default=str) + (result.error or "")


@pytest.mark.asyncio
async def test_missing_org_gives_connector_auth_failed_that_says_to_set_it(
    connector: VroConnector,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post(_ORG_LOGIN).respond(401)
        result = await _dispatch("vro.about", target=_Target(org=None))

    assert result.status == "error"
    assert result.extras["error_code"] == "connector_auth_failed"
    assert result.extras["remediation"].startswith("The target has no extras")


@pytest.mark.asyncio
async def test_no_token_in_result_log_line_audit_row_or_trace(
    connector: VroConnector, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = make_token({"exp": time.time() + 3600})
    target = _Target()
    with structlog.testing.capture_logs() as events:
        # Loggers made inside capture_logs always reach it, even when an
        # earlier test in the same worker cached the module loggers.
        for module in (vcfa_session, vro_connector_module):
            monkeypatch.setattr(module, "_log", structlog.get_logger(module.__name__))
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            mock.post(_ORG_LOGIN).mock(return_value=_session_answer(token))
            mock.get(_RUN_PATH).respond(200, json={"id": _RUN, "state": "completed"})
            result = await _dispatch(
                "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}, target
            )
            scope = capture._CaptureScope(audit_id=uuid4(), tenant_id=uuid4(), target_id=None)
            scope_token = capture._active_capture_var.set(scope)
            try:
                await connector._vro_get(target, _RUN_PATH, operator=_operator())
            finally:
                capture._active_capture_var.reset(scope_token)

    assert result.status == "ok", result.error
    assert {"vcfa_session_established", "vro_login_ready"} <= {e["event"] for e in events}
    assert result.audit_id is not None
    async with get_sessionmaker()() as session:
        row = (
            await session.execute(select(AuditLog).where(AuditLog.id == result.audit_id))
        ).scalar_one()
    audit = {column.name: getattr(row, column.name) for column in AuditLog.__table__.columns}
    spans = [span.attributes for span in scope.spans]
    assert spans, "the read must be traced"
    assert all("/cloudapi/" not in str(span.get("url")) for span in spans)
    for rendered in (
        json.dumps(result.result, default=str) + json.dumps(result.extras, default=str),
        json.dumps(events, default=str),
        json.dumps(audit, default=str),
        json.dumps(spans, default=str),
    ):
        assert token not in rendered
        assert _PASSWORD not in rendered


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def _workflow_get(connector: VroConnector, content: dict[str, Any]) -> dict[str, Any]:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_WORKFLOWS}/{_WF}").respond(200, json={"id": _WF, "name": "<name>"})
        mock.get(f"{_WORKFLOWS}/{_WF}/content").respond(200, json=content)
        result = await _dispatch("vro.workflow.get", {"workflow_id": _WF}, _Target(login=None))
    assert result.status == "ok", result.error
    body: dict[str, Any] = result.result
    return body


@pytest.mark.asyncio
async def test_content_checksum_is_stable_and_changes_with_the_content(
    connector: VroConnector,
) -> None:
    content = {
        "display-name": "<display-name>",
        "input": {
            "param": [
                {"name": "vmName", "type": "string", "description": "<input>"},
                {"name": "adminPassword", "type": "SecureString", "description": "<secret>"},
            ]
        },
        "schema": {"item": [{"name": "item1", "type": "task"}]},
    }
    reordered = dict(reversed(list(content.items())))
    changed = {**content, "schema": {"item": [{"name": "item1", "type": "link"}]}}

    first = await _workflow_get(connector, content)
    same = await _workflow_get(connector, reordered)
    other = await _workflow_get(connector, changed)

    assert len(first["content_sha256"]) == 64
    assert first["content_sha256"] == same["content_sha256"]
    assert first["content_sha256"] != other["content_sha256"]
    assert {p["name"]: p["secret"] for p in first["inputs"]} == {
        "vmName": False,
        "adminPassword": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "outcome"),
    [*sorted(OUTCOME_BY_STATE.items()), ("made-up-state", "unknown"), (None, "unknown")],
)
async def test_execution_get_gives_a_plain_outcome(
    connector: VroConnector, state: str | None, outcome: str
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).respond(200, json={"id": _RUN, "state": state})
        result = await _dispatch(
            "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}, _Target(login=None)
        )

    assert result.status == "ok", result.error
    assert result.result["state"] == state
    assert result.result["outcome"] == outcome


def test_every_vendor_run_state_has_an_outcome() -> None:
    vendor_states = {
        "canceled",
        "completed",
        "running",
        "suspended",
        "waiting",
        "waiting-signal",
        "failed",
        "initializing",
    }
    assert set(OUTCOME_BY_STATE) == vendor_states
    assert set(OUTCOME_BY_STATE.values()) <= {
        "completed",
        "failed",
        "canceled",
        "running",
        "waiting",
    }


@pytest.mark.asyncio
async def test_execution_get_says_not_reachable_when_the_server_does_not_answer(
    connector: VroConnector,
) -> None:
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).mock(side_effect=httpx.ConnectError("made-up refusal"))
        result = await _dispatch(
            "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}, _Target(login=None)
        )

    assert result.status == "ok", result.error
    assert result.result["outcome"] == "not_reachable"
    assert result.result["id"] == _RUN
    assert result.result["checked_at"]
    assert result.result["state"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("op_id", "path", "params", "envelope"),
    [
        ("vro.workflow.list", _WORKFLOWS, {}, lambda links: {"link": links}),
        (
            "vro.execution.list",
            _RUNS,
            {"workflow_id": _WF},
            lambda links: {"relations": {"link": links}},
        ),
    ],
    ids=["workflow-list", "execution-list"],
)
async def test_list_reads_say_whether_more_rows_exist(
    connector: VroConnector, op_id: str, path: str, params: dict[str, Any], envelope: Any
) -> None:
    links = [_link(id=f"row-{i}", name="<name>") for i in range(3)]

    def answer(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["maxResult"])
        return httpx.Response(200, json=envelope(links[:limit]))

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(path).mock(side_effect=answer)
        cut = await _dispatch(op_id, {**params, "max_results": 2}, _Target(login=None))
        whole = await _dispatch(op_id, {**params, "max_results": 3}, _Target(login=None))

    assert route.calls[0].request.url.params["maxResult"] == "3"
    assert cut.result["count"] == 2
    assert cut.result["more_rows"] is True
    assert whole.result["count"] == 3
    assert whole.result["more_rows"] is False


@pytest.mark.asyncio
async def test_log_read_says_whether_more_rows_exist(connector: VroConnector) -> None:
    lines = [{"severity": "info", "short-description": f"line {i}"} for i in range(3)]

    def answer(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["maxResult"])
        return httpx.Response(200, json={"logs": lines[:limit]})

    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_RUN_PATH}/syslogs").mock(side_effect=answer)
        cut = await _dispatch(
            "vro.execution.logs",
            {"workflow_id": _WF, "execution_id": _RUN, "max_lines": 2},
            _Target(login=None),
        )

    assert cut.result["more_rows"] is True
    assert cut.result["more_lines"] is True
