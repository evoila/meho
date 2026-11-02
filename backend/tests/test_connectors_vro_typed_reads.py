# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Orchestrator connector (``vro-rest``): login and the first seven reads.

What is covered, with a respx-mocked Orchestrator and no ingested catalog:

* the two logins: HTTP Basic (the default) and the Aria Automation token
  (``extras.login = automation``); a ``401`` drops the login of that
  target and the read then succeeds; automation targets share a login
  only when the login domain is the same; an unknown login mode stops early;
  a system or unauthenticated caller never reads or fills the login cache;
* every read through ``dispatch()``: ``vro.about``, ``vro.health``,
  ``vro.workflow.list`` (flat rows, the name and category conditions,
  paging), ``vro.workflow.get`` (both calls, no default values),
  ``vro.execution.list``, ``vro.execution.get`` (secret values replaced,
  also API key and secret key names, in parameters and Properties entries;
  a refused connection or a time-out gives ``outcome: not_reachable``, a
  certificate that is not trusted gives the normal certificate error, a
  destination MEHO refused gives the normal connector error)
  and ``vro.execution.logs`` (line and text caps; a labelled secret that
  crosses the text cut is removed before the cut);
* every call asks for JSON; a vendor error reaches the answer; an id with
  a ``..`` segment sends no request; a long list goes behind a result
  handle; no password or token in a result; registration (with the risk of
  every operation) and fingerprint.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
import ssl
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import wait_none

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors._shared.system_operator import synthesise_system_operator
from meho_backplane.connectors.adapters.http import SsrfBlockedError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.resolver import resolve_connector
from meho_backplane.connectors.vro import (
    VRO_CONNECTOR_ID,
    VRO_IMPL_ID,
    VRO_PRODUCT,
    VRO_TYPED_OPS,
    VRO_TYPED_WHEN_TO_USE_BY_GROUP,
    VRO_VERSION,
    VroConnector,
    register_vro_typed_operations,
)
from meho_backplane.connectors.vro.typed_reads import (
    LOG_TEXT_MAX,
    REDACTED,
    flat_rows,
    redact_secrets,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches, set_default_reducer
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations.jsonflux_reducer import JsonFluxReducer
from meho_backplane.settings import get_settings

_HOST = "vro-reads.test.invalid"
_BASE_URL = f"https://{_HOST}"
_PASSWORD = "vro-reads-password-value"
_TOKEN = "vro-reads-bearer-1"
_WF = "wf-0001"
_RUN = "run-0001"
_ABOUT = "/vco/api/about"
_WORKFLOWS = "/vco/api/workflows"
_RUNS = f"/vco/api/workflows/{_WF}/executions"
_RUN_PATH = f"/vco/api/workflows/{_WF}/executions/{_RUN}"
_ABOUT_PAYLOAD: dict[str, Any] = {
    "version": "8.18.1",
    "build-number": "11111111",
    "build-date": "2026-01-01",
    "api-version": "6.0.0",
    "id": "<server-id>",
    "time-zone": {"id": "UTC", "displayName": "UTC", "dstsavings": 0, "rawOffset": 0},
    "features": [{"name": "<feature>"}],
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
        product=VRO_PRODUCT, version=VRO_VERSION, impl_id=VRO_IMPL_ID, cls=VroConnector
    )
    register_connector_v2(product=VRO_PRODUCT, version="", impl_id="", cls=VroConnector)
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
    """Target satisfying ``VroTargetLike`` and the resolver shape."""

    def __init__(self, *, login: str | None = None, version: str = "8.18.1") -> None:
        self.product = VRO_PRODUCT
        self.fingerprint = type("_FP", (), {"version": version})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid4()
        self.tenant_id: UUID = UUID("00000000-0000-0000-0000-0000000000d0")
        self.name = f"vro-reads-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-reads/vro-reads"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {} if login is None else {"login": login}


def _operator() -> Operator:
    return Operator(
        sub="op-reads-vro",
        name="Orchestrator Reads Operator",
        email=None,
        raw_jwt="op.reads.vro.jwt",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


_loader = AsyncMock(return_value={"username": "vro-reads-svc", "password": _PASSWORD})


async def _register(_stub_embedding: AsyncMock) -> VroConnector:
    await register_vro_typed_operations()
    instance = get_or_create_connector_instance(VroConnector)
    assert isinstance(instance, VroConnector)
    _loader.reset_mock()
    instance._credentials_loader = _loader
    return instance


async def _dispatch(
    op_id: str, params: dict[str, Any] | None = None, target: _Target | None = None
) -> Any:
    return await dispatch(
        operator=_operator(),
        connector_id=VRO_CONNECTOR_ID,
        op_id=op_id,
        target=target or _Target(),
        params=params or {},
    )


def _link(**attributes: Any) -> dict[str, Any]:
    return {
        "rel": "down",
        "href": f"https://<orchestrator>/vco/api/x/{attributes.get('id')}/",
        "attributes": [{"name": key, "value": value} for key, value in attributes.items()],
    }


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_about_uses_basic_login_by_default_and_asks_for_json(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        result = await _dispatch("vro.about")

    assert result.status == "ok", result.error
    request = about.calls[0].request
    assert request.headers["authorization"].startswith("Basic ")
    assert request.headers["accept"] == "application/json"
    assert result.result == {
        "version": "8.18.1",
        "build_number": "11111111",
        "build_date": "2026-01-01",
        "api_version": "6.0.0",
        "id": "<server-id>",
        "time_zone": "UTC",
        "features": ["<feature>"],
    }


@pytest.mark.asyncio
async def test_automation_login_gets_a_token_and_a_401_logs_in_again(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    target = _Target(login="automation")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post("/csp/gateway/am/api/login").respond(
            200, json={"refresh_token": "made-up-refresh"}
        )
        iaas = mock.post("/iaas/api/login")
        iaas.side_effect = [
            httpx.Response(200, json={"tokenType": "Bearer", "token": _TOKEN}),
            httpx.Response(200, json={"tokenType": "Bearer", "token": "vro-reads-bearer-2"}),
        ]
        about = mock.get(_ABOUT)
        about.side_effect = [httpx.Response(401), httpx.Response(200, json=_ABOUT_PAYLOAD)]
        result = await _dispatch("vro.about", target=target)

    assert result.status == "ok", result.error
    assert csp.call_count == 2
    assert iaas.call_count == 2
    assert about.calls[0].request.headers["authorization"] == f"Bearer {_TOKEN}"
    assert about.calls[1].request.headers["authorization"] == "Bearer vro-reads-bearer-2"
    assert result.result["version"] == "8.18.1"


@pytest.mark.asyncio
async def test_basic_401_reads_the_login_again_and_the_read_then_succeeds(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    target = _Target()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT)
        about.side_effect = [
            httpx.Response(200, json=_ABOUT_PAYLOAD),
            httpx.Response(401),
            httpx.Response(200, json=_ABOUT_PAYLOAD),
        ]
        assert (await _dispatch("vro.about", target=target)).status == "ok"
        result = await _dispatch("vro.about", target=target)

    assert result.status == "ok", result.error
    assert about.call_count == 3
    assert _loader.await_count == 2


@pytest.mark.asyncio
async def test_unknown_login_mode_stops_before_any_request(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        result = await _dispatch("vro.about", target=_Target(login="kerberos"))

    assert result.status == "error"
    assert about.call_count == 0
    assert "extras.login" in json.dumps(result.extras, default=str) + (result.error or "")


def _unauthenticated_operator() -> Operator:
    return Operator(
        sub="op-reads-vro-no-jwt",
        name=None,
        email=None,
        raw_jwt="",
        tenant_id=UUID("00000000-0000-0000-0000-0000000000d4"),
        tenant_role=TenantRole.OPERATOR,
    )


@pytest.mark.asyncio
async def test_system_caller_does_not_reuse_a_cached_basic_login() -> None:
    """A warm Basic login is served only to a caller with an operator JWT.

    The system operator and an operator without a JWT run the loader
    again (the Vault loader fails closed for them) and leave the cache as
    it was; the real operator still reuses its own login afterwards.
    """
    callers: list[str] = []

    async def loader(target: Any, operator: Operator) -> dict[str, str]:
        callers.append(operator.sub)
        return {"username": "vro-reads-svc", "password": _PASSWORD}

    connector = VroConnector(credentials_loader=loader)
    target = _Target()
    warm = await connector.auth_headers(target, _operator())
    assert await connector.auth_headers(target, _operator()) == warm
    assert callers == ["op-reads-vro"]

    await connector.auth_headers(target, synthesise_system_operator())
    await connector.auth_headers(target, _unauthenticated_operator())
    assert callers == ["op-reads-vro", "system:connector-probe", "op-reads-vro-no-jwt"]

    assert await connector.auth_headers(target, _operator()) == warm
    assert len(callers) == 3
    assert len(connector._authorizations) == 1


@pytest.mark.asyncio
async def test_system_caller_does_not_reuse_a_cached_automation_login() -> None:
    """A warm Aria Automation token is not handed to the system operator.

    The system caller logs in again and gets its own token, which is not
    kept; the real operator keeps using the first token.
    """
    callers: list[str] = []

    async def loader(target: Any, operator: Operator) -> dict[str, str]:
        callers.append(operator.sub)
        return {"username": "vro-reads-svc", "password": _PASSWORD}

    connector = VroConnector(credentials_loader=loader)
    target = _Target(login="automation")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post("/csp/gateway/am/api/login").respond(
            200, json={"refresh_token": "made-up-refresh"}
        )
        iaas = mock.post("/iaas/api/login")
        iaas.side_effect = [
            httpx.Response(200, json={"tokenType": "Bearer", "token": _TOKEN}),
            httpx.Response(200, json={"tokenType": "Bearer", "token": "vro-reads-bearer-2"}),
        ]
        warm = await connector.auth_headers(target, _operator())
        system = await connector.auth_headers(target, synthesise_system_operator())
        again = await connector.auth_headers(target, _operator())

    assert warm == {"Authorization": f"Bearer {_TOKEN}"}
    assert system == {"Authorization": "Bearer vro-reads-bearer-2"}
    assert again == warm
    assert iaas.call_count == 2
    assert callers == ["op-reads-vro", "system:connector-probe"]


@pytest.mark.asyncio
async def test_automation_targets_share_a_login_only_when_the_domain_is_the_same(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """One secret and one host: the same domain shares one login, another domain logs in alone."""
    await _register(_stub_embedding)
    first, second, other = (_Target(login="automation") for _ in range(3))
    first.extras["domain"] = "domain-a"
    second.extras["domain"] = "domain-a"
    other.extras["domain"] = "domain-b"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        csp = mock.post("/csp/gateway/am/api/login").respond(
            200, json={"refresh_token": "made-up-refresh"}
        )
        iaas = mock.post("/iaas/api/login")
        iaas.side_effect = [
            httpx.Response(200, json={"tokenType": "Bearer", "token": _TOKEN}),
            httpx.Response(200, json={"tokenType": "Bearer", "token": "vro-reads-bearer-2"}),
        ]
        about = mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        for target in (first, second, other):
            assert (await _dispatch("vro.about", target=target)).status == "ok"

    assert [json.loads(call.request.content)["domain"] for call in csp.calls] == [
        "domain-a",
        "domain-b",
    ]
    assert [call.request.headers["authorization"] for call in about.calls] == [
        f"Bearer {_TOKEN}",
        f"Bearer {_TOKEN}",
        "Bearer vro-reads-bearer-2",
    ]


# ---------------------------------------------------------------------------
# Server reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_returns_state_and_one_row_per_check(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    payload = {
        "serverState": "RUNNING",
        "health-status": {
            "healthStatus": "WARNING",
            "description": "<text>",
            "time": 1700000000000,
            "health-status-providers": [
                {
                    "name": "<check>",
                    "state": "WARNING",
                    "critical": False,
                    "message": "<message>",
                    "descritpion": "<what it checks>",
                }
            ],
        },
        "instance-id": "<instance>",
        "local-configuration-descriptor": "<config>",
        "pending-restart-in": 0,
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        health = mock.get("/vco/api/healthstatus").respond(200, json=payload)
        result = await _dispatch("vro.health")

    assert result.status == "ok", result.error
    assert health.calls[0].request.url.params["showDetails"] == "true"
    body = result.result
    assert body["server_state"] == "RUNNING"
    assert body["health_status"] == "WARNING"
    assert body["providers"] == [
        {
            "name": "<check>",
            "state": "WARNING",
            "critical": False,
            "message": "<message>",
            "description": "<what it checks>",
        }
    ]
    assert "local-configuration-descriptor" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_workflow_list_returns_flat_rows_and_sends_the_conditions(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    payload = {
        "startIndex": 0,
        "count": 1,
        "total": 1,
        "link": [_link(id=_WF, name="<workflow-name>", categoryName="<category>", version="1.0")],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(_WORKFLOWS).respond(200, json=payload)
        result = await _dispatch(
            "vro.workflow.list", {"name": "<part>", "category": "<category>", "max_results": 50}
        )

    assert result.status == "ok", result.error
    params = route.calls[0].request.url.params
    assert params.get_list("conditions") == ["name~<part>", "categoryName=<category>"]
    assert params["maxResult"] == "51"
    assert params["startIndex"] == "0"
    body = result.result
    assert body["total"] == 1
    assert body["count"] == 1
    assert body["more_rows"] is False
    assert body["items"][0]["id"] == _WF
    assert body["items"][0]["categoryName"] == "<category>"
    assert body["items"][0]["href"].endswith(f"/{_WF}/")


def test_flat_rows_skips_links_without_attributes() -> None:
    assert flat_rows([{"href": "x"}, "y", _link(id="a")]) == [
        {"id": "a", "href": "https://<orchestrator>/vco/api/x/a/"}
    ]
    assert flat_rows(None) == []


@pytest.mark.asyncio
async def test_workflow_get_reads_both_calls_and_returns_no_default_values(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    workflow = {
        "id": _WF,
        "name": "<workflow-name>",
        "version": "1.0.0",
        "description": "<description>",
        "category-id": "<category-id>",
        "inputParameters": [{"name": "vmName", "type": "string", "value": {"x": "made-up"}}],
    }
    content = {
        "display-name": "<display-name>",
        "input": {
            "param": [
                {"name": "vmName", "type": "string", "description": "<input>"},
                {"name": "adminPassword", "type": "SecureString", "description": "<secret>"},
            ]
        },
        "output": {"param": [{"name": "result", "type": "string", "description": "<out>"}]},
        "attrib": [{"name": "apiKey", "type": "string", "value": {"string": {"value": _PASSWORD}}}],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_WORKFLOWS}/{_WF}").respond(200, json=workflow)
        mock.get(f"{_WORKFLOWS}/{_WF}/content").respond(200, json=content)
        result = await _dispatch("vro.workflow.get", {"workflow_id": _WF})

    assert result.status == "ok", result.error
    body = result.result
    assert body["display_name"] == "<display-name>"
    assert body["category_id"] == "<category-id>"
    assert [p["name"] for p in body["inputs"]] == ["vmName", "adminPassword"]
    assert body["outputs"] == [{"name": "result", "type": "string", "description": "<out>"}]
    assert _PASSWORD not in json.dumps(body)
    assert "made-up" not in json.dumps(body)


# ---------------------------------------------------------------------------
# Workflow runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execution_list_returns_one_flat_row_per_run(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    payload = {
        "relations": {
            "startIndex": 0,
            "total": 2,
            "link": [
                _link(id=_RUN, state="failed", startDate="<start>", startedBy="<user>"),
                _link(id="run-0002", state="completed", startDate="<start>"),
            ],
        },
        "href": "https://<orchestrator>/vco/api/workflows/wf-0001/executions/",
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(_RUNS).respond(200, json=payload)
        result = await _dispatch("vro.execution.list", {"workflow_id": _WF, "start_index": 10})

    assert result.status == "ok", result.error
    assert route.calls[0].request.url.params["startIndex"] == "10"
    body = result.result
    assert body["total"] == 2
    assert [row["state"] for row in body["items"]] == ["failed", "completed"]
    assert body["items"][0]["startedBy"] == "<user>"


@pytest.mark.asyncio
async def test_execution_get_returns_state_error_and_replaces_secret_values(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    payload = {
        "id": _RUN,
        "name": "<workflow-name>",
        "state": "failed",
        "business-state": "<step>",
        "start-date": "<start>",
        "end-date": "<end>",
        "started-by": "<user>",
        "content-exception": "<error text>",
        "current-item-display-name": "<item>",
        "inputParameters": [
            {"name": "vmName", "type": "string", "value": {"string": {"value": "<vm-name>"}}},
            {"name": "rootPw", "type": "SecureString", "value": {"string": {"value": _PASSWORD}}},
            {"name": "opaque", "type": "string", "encrypt-value": True, "value": "made-up-cipher"},
            {"name": "apiToken", "type": "string", "value": {"string": {"value": "made-up-tok"}}},
        ],
        "outputParameters": [],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).respond(200, json=payload)
        result = await _dispatch("vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN})

    assert result.status == "ok", result.error
    body = result.result
    assert body["state"] == "failed"
    assert body["error"] == "<error text>"
    assert body["current_item"] == "<item>"
    values = {p["name"]: p["value"] for p in body["inputs"]}
    assert values["vmName"] == {"string": {"value": "<vm-name>"}}
    assert values["rootPw"] == REDACTED
    assert values["opaque"] == REDACTED
    assert values["apiToken"] == REDACTED
    rendered = json.dumps(body)
    for secret in (_PASSWORD, "made-up-cipher", "made-up-tok"):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_execution_get_replaces_secret_values_in_properties_entries(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    connector = await _register(_stub_embedding)
    payload = {
        "id": _RUN,
        "state": "completed",
        "inputParameters": [
            {
                "name": "inputProperties",
                "type": "Properties",
                "value": {
                    "properties": {
                        "property": [
                            {"key": "hostName", "value": {"string": {"value": "<host-name>"}}},
                            {"key": "adminPassword", "value": {"string": {"value": _PASSWORD}}},
                            {
                                "key": "customProperties",
                                "value": {
                                    "properties": {
                                        "property": [
                                            {
                                                "key": "api_token",
                                                "value": {"string": {"value": "made-up-tok"}},
                                            }
                                        ]
                                    }
                                },
                            },
                        ]
                    }
                },
            }
        ],
        "outputParameters": [],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).respond(200, json=payload)
        result = await _dispatch("vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN})

    assert result.status == "ok", result.error
    entries = result.result["inputs"][0]["value"]["properties"]["property"]
    assert entries[0]["value"] == {"string": {"value": "<host-name>"}}
    assert entries[1]["value"] == REDACTED
    nested = entries[2]["value"]["properties"]["property"]
    assert nested[0]["value"] == REDACTED
    trace = connector.trace_response_body(httpx.Response(200, json=payload))
    for rendered in (json.dumps(result.result), json.dumps(trace)):
        assert "<host-name>" in rendered
        for secret in (_PASSWORD, "made-up-tok"):
            assert secret not in rendered


def _tls_verify_connect_error() -> httpx.ConnectError:
    """The error httpx raises when the server answers with an untrusted certificate."""
    message = "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed certificate"
    error = httpx.ConnectError(message)
    error.__cause__ = ssl.SSLCertVerificationError(1, message)
    return error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "transport_error",
    [httpx.ConnectError("[Errno 111] Connection refused"), httpx.ReadTimeout("timed out")],
    ids=["refused", "timeout"],
)
async def test_execution_get_is_not_reachable_when_the_server_does_not_answer(
    _stub_embedding: AsyncMock, session: AsyncSession, transport_error: httpx.TransportError
) -> None:
    connector = await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(_RUN_PATH).mock(side_effect=transport_error)
        with patch.object(
            connector._request_json.retry,
            "wait",
            wait_none(),
        ):
            result = await _dispatch(
                "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}
            )

    assert result.status == "ok", result.error
    assert route.called
    body = result.result
    assert body["outcome"] == "not_reachable"
    assert body["id"] == _RUN
    assert body["checked_at"]
    assert body["state"] is None
    assert body["error"] is None
    assert body["inputs"] == []


@pytest.mark.asyncio
async def test_execution_get_keeps_the_certificate_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """An untrusted certificate is not "not reachable": the server answered."""
    connector = await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).mock(side_effect=_tls_verify_connect_error())
        with patch.object(
            connector._request_json.retry,
            "wait",
            wait_none(),
        ):
            result = await _dispatch(
                "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}
            )

    assert result.status == "error"
    assert result.error is not None
    assert result.error.startswith("connector_tls_verify_failed:")
    assert result.extras["error_code"] == "connector_tls_verify_failed"
    assert result.extras["host"] == _HOST
    assert "tls_ca_pin" in result.error
    assert "not_reachable" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_execution_get_keeps_the_blocked_destination_error(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    """A destination MEHO refused is not "not reachable": no request was sent."""
    connector = await _register(_stub_embedding)
    guard_message = "target host resolves to a private address; dispatch refused"
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(_RUN_PATH).mock(side_effect=SsrfBlockedError(guard_message))
        with patch.object(
            connector._request_json.retry,
            "wait",
            wait_none(),
        ):
            result = await _dispatch(
                "vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN}
            )

    assert result.status == "error"
    assert result.error == "connector_error: SsrfBlockedError"
    assert result.extras["error_code"] == "connector_error"
    assert result.extras["exception_message"] == guard_message
    assert route.call_count == 1, "a refused destination is not tried again"
    assert "not_reachable" not in json.dumps(result.result)


@pytest.mark.asyncio
async def test_api_key_and_secret_key_values_are_replaced_in_parameters_and_properties(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    connector = await _register(_stub_embedding)
    secrets = {
        "apiKey": "made-up-api-key-1",
        "secretKey": "made-up-secret-key-1",
        "vcenterApiKey": "made-up-api-key-2",
        "aws_secret_key": "made-up-secret-key-2",
    }
    payload = {
        "id": _RUN,
        "state": "completed",
        "inputParameters": [
            {"name": "vmName", "type": "string", "value": {"string": {"value": "<vm-name>"}}},
            {"name": "apiKey", "type": "string", "value": {"string": {"value": secrets["apiKey"]}}},
            {
                "name": "secretKey",
                "type": "string",
                "value": {"string": {"value": secrets["secretKey"]}},
            },
            {
                "name": "inputProperties",
                "type": "Properties",
                "value": {
                    "properties": {
                        "property": [
                            {"key": "region", "value": {"string": {"value": "<region>"}}},
                            {
                                "key": "vcenterApiKey",
                                "value": {"string": {"value": secrets["vcenterApiKey"]}},
                            },
                            {
                                "key": "nested",
                                "value": {
                                    "properties": {
                                        "property": [
                                            {
                                                "key": "aws_secret_key",
                                                "value": {
                                                    "string": {"value": secrets["aws_secret_key"]}
                                                },
                                            }
                                        ]
                                    }
                                },
                            },
                        ]
                    }
                },
            },
        ],
        "outputParameters": [
            {"name": "apiKey", "type": "string", "value": {"string": {"value": "made-up-out-1"}}}
        ],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).respond(200, json=payload)
        result = await _dispatch("vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN})

    assert result.status == "ok", result.error
    inputs = {p["name"]: p["value"] for p in result.result["inputs"]}
    assert inputs["vmName"] == {"string": {"value": "<vm-name>"}}
    assert inputs["apiKey"] == REDACTED
    assert inputs["secretKey"] == REDACTED
    entries = inputs["inputProperties"]["properties"]["property"]
    assert entries[0]["value"] == {"string": {"value": "<region>"}}
    assert entries[1]["value"] == REDACTED
    assert entries[2]["value"]["properties"]["property"][0]["value"] == REDACTED
    assert result.result["outputs"][0]["value"] == REDACTED
    trace = connector.trace_response_body(httpx.Response(200, json=payload))
    for rendered in (json.dumps(result.result), json.dumps(trace)):
        assert "<region>" in rendered
        for secret in (*secrets.values(), "made-up-out-1"):
            assert secret not in rendered


@pytest.mark.parametrize(
    "name",
    [
        "passphrase",
        "sshPassphrase",
        "privateKeyPem",
        "private_key_pem",
        "passwordValue",
        "PasswordValue",
    ],
)
def test_shared_secret_names_are_replaced_at_any_depth(name: str) -> None:
    secret = "made-up-secret-value"
    payload = {
        "inputParameters": [
            {"name": name, "type": "string", "value": {"string": {"value": secret}}},
            {
                "name": "props",
                "type": "Properties",
                "value": {"properties": {"property": [{"key": name, "value": secret}]}},
            },
        ],
        name: secret,
        "nested": {"inner": {name: secret}},
        "keep": "<kept-value>",
    }
    redacted = redact_secrets(payload)
    assert redacted["inputParameters"][0]["value"] == REDACTED
    assert redacted["inputParameters"][1]["value"]["properties"]["property"][0]["value"] == REDACTED
    assert redacted[name] == REDACTED
    assert redacted["nested"]["inner"][name] == REDACTED
    rendered = json.dumps(redacted)
    assert "<kept-value>" in rendered
    assert secret not in rendered
    assert json.dumps(payload).count(secret) == 4, "the input is not changed"


@pytest.mark.asyncio
async def test_passphrase_private_key_and_password_value_are_replaced_in_run_and_trace(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    connector = await _register(_stub_embedding)
    secrets = {
        "passphrase": "made-up-passphrase-1",
        "privateKeyPem": "made-up-private-key-1",
        "passwordValue": "made-up-password-1",
        "out_passphrase": "made-up-passphrase-2",
        "out_private_key": "made-up-private-key-2",
    }
    payload = {
        "id": _RUN,
        "state": "completed",
        "inputParameters": [
            {"name": "vmName", "type": "string", "value": {"string": {"value": "<vm-name>"}}},
            {
                "name": "passphrase",
                "type": "string",
                "value": {"string": {"value": secrets["passphrase"]}},
            },
            {
                "name": "privateKeyPem",
                "type": "string",
                "value": {"string": {"value": secrets["privateKeyPem"]}},
            },
            {
                "name": "inputProperties",
                "type": "Properties",
                "value": {
                    "properties": {
                        "property": [
                            {"key": "hostName", "value": {"string": {"value": "<host-name>"}}},
                            {
                                "key": "passwordValue",
                                "value": {"string": {"value": secrets["passwordValue"]}},
                            },
                        ]
                    }
                },
            },
        ],
        "outputParameters": [
            {"name": "result", "type": "string", "value": {"string": {"value": "<result>"}}},
            {
                "name": "passphrase",
                "type": "string",
                "value": {"string": {"value": secrets["out_passphrase"]}},
            },
            {
                "name": "privateKeyPem",
                "type": "string",
                "value": {"string": {"value": secrets["out_private_key"]}},
            },
        ],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_RUN_PATH).respond(200, json=payload)
        result = await _dispatch("vro.execution.get", {"workflow_id": _WF, "execution_id": _RUN})

    assert result.status == "ok", result.error
    inputs = {p["name"]: p["value"] for p in result.result["inputs"]}
    assert inputs["vmName"] == {"string": {"value": "<vm-name>"}}
    assert inputs["passphrase"] == REDACTED
    assert inputs["privateKeyPem"] == REDACTED
    entries = inputs["inputProperties"]["properties"]["property"]
    assert entries[0]["value"] == {"string": {"value": "<host-name>"}}
    assert entries[1]["value"] == REDACTED
    outputs = {p["name"]: p["value"] for p in result.result["outputs"]}
    assert outputs["result"] == {"string": {"value": "<result>"}}
    assert outputs["passphrase"] == REDACTED
    assert outputs["privateKeyPem"] == REDACTED
    trace = connector.trace_response_body(httpx.Response(200, json=payload))
    for rendered in (json.dumps(result.result), json.dumps(trace)):
        assert "<host-name>" in rendered
        assert "<result>" in rendered
        for secret in secrets.values():
            assert secret not in rendered


def _syslogs_answer(lines: list[dict[str, Any]]) -> Any:
    """A syslogs mock that sends no more lines than ``maxResult`` asks for."""

    def answer(request: httpx.Request) -> httpx.Response:
        limit = int(request.url.params["maxResult"])
        return httpx.Response(200, json={"logs": lines[:limit]})

    return answer


@pytest.mark.asyncio
async def test_execution_logs_cap_lines_and_text(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    long_text = "x" * (LOG_TEXT_MAX + 50)
    lines = [
        {
            "severity": "error",
            "userName": "<user>",
            "origin": "<origin>",
            "short-description": "<short>",
            "long-description": long_text,
            "time-stamp": "<time>",
            "time-stamp-val": 0,
        },
        {"entry": {"severity": "info", "short-description": "<wrapped>"}},
        {"severity": "info", "short-description": "<third>"},
        {"severity": "info", "short-description": "<fourth>"},
    ]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(f"{_RUN_PATH}/syslogs").mock(side_effect=_syslogs_answer(lines))
        result = await _dispatch(
            "vro.execution.logs", {"workflow_id": _WF, "execution_id": _RUN, "max_lines": 2}
        )

    assert result.status == "ok", result.error
    assert route.calls[0].request.url.params["maxResult"] == "3"
    body = result.result
    assert body["lines"] == 2
    assert body["more_lines"] is True
    first, second = body["items"]
    assert first["user"] == "<user>"
    assert len(first["long_description"]) == LOG_TEXT_MAX
    assert first["cut"] is True
    assert second["short_description"] == "<wrapped>"
    assert second["cut"] is False


@pytest.mark.asyncio
async def test_execution_logs_with_exactly_max_lines_has_no_more_lines(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    lines = [{"severity": "info", "short-description": f"<line-{i}>"} for i in range(2)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_RUN_PATH}/syslogs").mock(side_effect=_syslogs_answer(lines))
        result = await _dispatch(
            "vro.execution.logs", {"workflow_id": _WF, "execution_id": _RUN, "max_lines": 2}
        )

    assert result.status == "ok", result.error
    assert result.result["lines"] == 2
    assert result.result["more_lines"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["short-description", "long-description"])
async def test_execution_logs_remove_a_secret_that_crosses_the_cut(
    _stub_embedding: AsyncMock, session: AsyncSession, field: str
) -> None:
    """A labelled secret that straddles the text cut is removed before the cut.

    Cut first, only five characters of the value would remain; the shared
    rules need at least eight and would miss them.
    """
    await _register(_stub_embedding)
    secret = "vro-log-secret-value-0001"
    label = "password: "
    prefix = "x" * (LOG_TEXT_MAX - len(label) - 6) + " "
    text = f"{prefix}{label}{secret} {'y' * 100}"
    assert text[:LOG_TEXT_MAX].endswith(label + secret[:5])
    lines = [{"severity": "info", field: text}]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_RUN_PATH}/syslogs").mock(side_effect=_syslogs_answer(lines))
        result = await _dispatch(
            "vro.execution.logs", {"workflow_id": _WF, "execution_id": _RUN, "max_lines": 1}
        )

    assert result.status == "ok", result.error
    row = result.result["items"][0]
    kept = row[field.replace("-", "_")]
    assert len(kept) == LOG_TEXT_MAX
    assert row["cut"] is True
    assert kept.startswith(prefix + "[REDACTED:")
    assert secret[:5] not in json.dumps(result.result)


# ---------------------------------------------------------------------------
# Errors, ids, result handle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vendor_error_reaches_the_answer(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(f"{_WORKFLOWS}/{_WF}").respond(404, json={"message": "workflow not found"})
        mock.get(f"{_WORKFLOWS}/{_WF}/content").respond(404)
        result = await _dispatch("vro.workflow.get", {"workflow_id": _WF})

    assert result.status == "error"
    assert "404" in (result.error or "") + json.dumps(result.extras, default=str)


@pytest.mark.asyncio
async def test_id_with_a_dot_segment_sends_no_request(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.route()
        result = await _dispatch("vro.execution.list", {"workflow_id": "../about"})

    assert result.status == "error"
    assert route.call_count == 0


@pytest.mark.asyncio
async def test_id_with_a_slash_stays_one_path_segment(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get(path__regex=r".*/executions$").respond(200, json={"relations": {}})
        result = await _dispatch("vro.execution.list", {"workflow_id": "a/b"})

    assert result.status == "ok", result.error
    assert route.calls[0].request.url.raw_path.startswith(b"/vco/api/workflows/a%2Fb/")


@pytest.mark.asyncio
async def test_long_workflow_list_goes_behind_a_result_handle(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    set_default_reducer(JsonFluxReducer())
    links = [_link(id=f"wf-{i:04d}", name="<workflow-name>") for i in range(120)]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_WORKFLOWS).respond(200, json={"total": 120, "startIndex": 0, "link": links})
        result = await _dispatch("vro.workflow.list")

    assert result.status == "ok", result.error
    assert result.handle is not None
    assert result.result["row_count"] == 120
    assert result.result["total"] == 120


@pytest.mark.asyncio
async def test_results_hold_no_password_or_token(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register(_stub_embedding)
    target = _Target(login="automation")
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.post("/csp/gateway/am/api/login").respond(200, json={"refresh_token": "made-up-r"})
        mock.post("/iaas/api/login").respond(200, json={"token": _TOKEN})
        mock.get(_RUNS).respond(
            200,
            json={"relations": {"link": [_link(id=_RUN, password="made-up-pw", token="t-1")]}},
        )
        result = await _dispatch("vro.execution.list", {"workflow_id": _WF}, target)

    assert result.status == "ok", result.error
    rendered = json.dumps(result.result) + json.dumps(result.extras, default=str)
    for secret in (_PASSWORD, _TOKEN, "made-up-r", "made-up-pw", "t-1"):
        assert secret not in rendered


# ---------------------------------------------------------------------------
# Registration and fingerprint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ops_are_registered_typed_and_grouped_with_their_risk(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await register_vro_typed_operations()
    rows = {
        row.op_id: row
        for row in (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == VRO_PRODUCT,
                    EndpointDescriptor.impl_id == VRO_IMPL_ID,
                )
            )
        )
        .scalars()
        .all()
    }
    expected = {
        "vro.about",
        "vro.health",
        "vro.workflow.list",
        "vro.workflow.get",
        "vro.execution.list",
        "vro.execution.get",
        "vro.execution.logs",
        "vro.action.list",
        "vro.config_element.list",
        "vro.package.list",
        "vro.workflow.run",
        "vro.execution.cancel",
    }
    changes = {"vro.workflow.run": "destructive", "vro.execution.cancel": "dangerous"}
    assert {op.op_id for op in VRO_TYPED_OPS} == expected
    assert expected <= set(rows)
    for op in VRO_TYPED_OPS:
        row = rows[op.op_id]
        assert row.source_kind == "typed"
        if op.op_id in changes:
            assert row.safety_level == changes[op.op_id]
            assert row.requires_approval is True
            assert "write" in row.tags
            assert row.llm_instructions["preconditions"]
        else:
            assert row.safety_level == "safe"
            assert row.requires_approval is False
            assert "read-only" in row.tags
        assert row.parameter_schema["examples"]
        assert op.group_key in VRO_TYPED_WHEN_TO_USE_BY_GROUP
        assert "\n" not in row.summary and len(row.description) <= 300


@pytest.mark.parametrize("version", ["8.18.1", "9.0.0", "9.1.1", None])
def test_targets_of_each_version_resolve_to_this_connector(version: str | None) -> None:
    target = _Target(version=version or "")
    if version is None:
        target.fingerprint = None  # type: ignore[assignment]
    assert resolve_connector(target) is VroConnector


@pytest.mark.asyncio
async def test_fingerprint_reports_version_and_build() -> None:
    connector = VroConnector(credentials_loader=_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_ABOUT).respond(200, json=_ABOUT_PAYLOAD)
        fp = await connector.fingerprint(_Target())
    assert fp.reachable is True
    assert fp.product == "vro"
    assert fp.version == "8.18.1"
    assert fp.build == "11111111"


@pytest.mark.asyncio
async def test_fingerprint_failure_is_not_reachable_and_names_the_error() -> None:
    connector = VroConnector(credentials_loader=_loader)
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_ABOUT).respond(401)
        fp = await connector.fingerprint(_Target())
    assert fp.reachable is False
    assert "401" in fp.extras["error"]
