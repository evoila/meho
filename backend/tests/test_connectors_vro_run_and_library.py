# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the Orchestrator changes and library reads (``vro-rest``).

What is covered, with a respx-mocked Orchestrator and no ingested catalog:

* ``vro.action.list`` (flat rows, the module filter, ``more_rows``),
  ``vro.config_element.list`` (the list, and one element with its secret
  values replaced) and ``vro.package.list``;
* input values wrapped the way the vendor expects, and refused when they
  do not fit;
* the ``vro.workflow.run`` preview: the card with ``blast_radius``, secret
  inputs as Vault references only, refused for a missing workflow or a
  secret given as a value;
* the governed flow: a run without a matching ``preview_hash`` is refused;
  preview, wait, a second person approves, the run starts once; no secret
  value in the approval row, the audit rows, the answer or the trace;
* the approved run: ``precondition_failed`` when the card does not match
  or no card is bound, ``already_running`` when a run still goes, the run
  id from the body or the ``Location`` header, a vendor error with its
  text (withheld when it holds a secret);
* the status read after a lost login: a new login, then the state;
* ``vro.execution.cancel``: waits for approval, cancels a running run, a
  run that ended is ``unchanged``, a vendor error comes back.

Fixture values are made up; no real host, user or id appears here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorUpstreamError
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vro import (
    VRO_CONNECTOR_ID,
    VRO_IMPL_ID,
    VRO_PRODUCT,
    VRO_VERSION,
    VroConnector,
    register_vro_typed_operations,
)
from meho_backplane.connectors.vro.run_writes import (
    RUN_OP_ID,
    VroRunRefusedError,
    wrap_value,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, AuditLog
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import (
    get_or_create_connector_instance,
    reset_handler_cache,
)
from meho_backplane.operations._preview import (
    _PREVIEW_BUILDERS,
    PreviewContext,
    approved_preview_var,
)
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation, preview_operation
from meho_backplane.settings import get_settings

_HOST = "vro-run.test.invalid"
_BASE_URL = f"https://{_HOST}"
_TENANT = UUID("00000000-0000-0000-0000-0000000045a6")
_PASSWORD = "vro-run-login-password"
_SECRET = "vro-run-secret-input-value"
_WF = "wf-0001"
_RUN = "run-0001"
_WORKFLOW = f"/vco/api/workflows/{_WF}"
_CONTENT = f"/vco/api/workflows/{_WF}/content"
_EXECUTIONS = f"/vco/api/workflows/{_WF}/executions"
_STATE = f"/vco/api/workflows/{_WF}/executions/{_RUN}/state"
_RUN_PATH = f"/vco/api/workflows/{_WF}/executions/{_RUN}"
_WORKFLOW_PAYLOAD: dict[str, Any] = {
    "id": _WF,
    "name": "<workflow-name>",
    "version": "1.0.0",
    "description": "<description>",
}
_CONTENT_PAYLOAD: dict[str, Any] = {
    "display-name": "<workflow-name>",
    "input": {
        "param": [
            {"name": "vmName", "type": "string"},
            {"name": "adminPassword", "type": "SecureString"},
            {"name": "count", "type": "number"},
            {"name": "vm", "type": "VC:VirtualMachine"},
            {"name": "tags", "type": "Array/string"},
        ]
    },
    "output": {"param": []},
}
_INPUTS: list[dict[str, Any]] = [
    {"name": "vmName", "value": "<vm-name>"},
    {"name": "adminPassword", "secret_ref": "<vault-path>#password"},
    {"name": "count", "value": 2},
    {"name": "vm", "value": "<vcenter>/vm-1"},
    {"name": "tags", "value": ["a", "b"]},
]


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
    builders = dict(_PREVIEW_BUILDERS)
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
    _PREVIEW_BUILDERS.clear()
    _PREVIEW_BUILDERS.update(builders)


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
    monkeypatch.setattr("meho_backplane.operations._search.get_embedding_service", lambda: service)
    return service


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as s:
        yield s


@pytest.fixture
def _vault() -> Iterator[AsyncMock]:
    """The Vault read of a secret input; returns the made-up secret."""
    read = AsyncMock(return_value={"password": _SECRET})
    with patch("meho_backplane.connectors.vro.run_writes.load_param_secret_data", read):
        yield read


class _Target:
    """Target satisfying ``VroTargetLike`` and the resolver shape."""

    def __init__(self) -> None:
        self.product = VRO_PRODUCT
        self.fingerprint = type("_FP", (), {"version": "9.1.1"})()
        self.preferred_impl_id: str | None = None
        self.id: UUID = uuid4()
        self.tenant_id: UUID = _TENANT
        self.name = f"vro-run-{self.id.hex[:6]}"
        self.host = _HOST
        self.port = 443
        self.secret_ref = "targets/op-run/vro-run"
        self.auth_model = "shared_service_account"
        self.extras: dict[str, Any] = {}


def _operator(sub: str = "op-run-vro") -> Operator:
    return Operator(
        sub=sub,
        name="Orchestrator Run Operator",
        email=None,
        raw_jwt="op.run.vro.jwt",
        tenant_id=_TENANT,
        tenant_role=TenantRole.OPERATOR,
    )


_loader = AsyncMock(return_value={"username": "vro-run-svc", "password": _PASSWORD})


async def _register() -> VroConnector:
    await register_vro_typed_operations()
    instance = get_or_create_connector_instance(VroConnector)
    assert isinstance(instance, VroConnector)
    instance._credentials_loader = _loader
    return instance


def _link(**attributes: Any) -> dict[str, Any]:
    return {
        "rel": "down",
        "href": f"https://<orchestrator>/vco/api/x/{attributes.get('id')}/",
        "attributes": [{"name": key, "value": value} for key, value in attributes.items()],
    }


def _mock_workflow(mock: respx.MockRouter) -> None:
    mock.get(_WORKFLOW).respond(200, json=_WORKFLOW_PAYLOAD)
    mock.get(_CONTENT).respond(200, json=_CONTENT_PAYLOAD)


def _mock_runs(mock: respx.MockRouter, *states: str) -> respx.Route:
    links = [_link(id=f"run-{i}", state=state) for i, state in enumerate(states)]
    return mock.get(_EXECUTIONS).respond(200, json={"relations": {"link": links}})


async def _card(connector: VroConnector, params: dict[str, Any]) -> dict[str, Any]:
    """Run the preview builder as the dispatcher does at park time."""
    ctx = PreviewContext(
        descriptor=type("_D", (), {"op_id": RUN_OP_ID})(),  # type: ignore[arg-type]
        connector_instance=connector,
        operator=_operator(),
        target=_Target(),
        params=params,
    )
    card = await _PREVIEW_BUILDERS[RUN_OP_ID](ctx)
    assert card is not None
    return card


async def _approved_run(
    connector: VroConnector, params: dict[str, Any], card: dict[str, Any] | None
) -> dict[str, Any]:
    """Call the run handler as the approved re-run does, with *card* bound."""
    token = approved_preview_var.set(None if card is None else (RUN_OP_ID, card))
    try:
        return await connector.workflow_run(_operator(), _Target(), params)
    finally:
        approved_preview_var.reset(token)


# ---------------------------------------------------------------------------
# Library reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_action_list_keeps_one_module_and_says_when_more_rows_exist(
    session: AsyncSession,
) -> None:
    await _register()
    links = [
        _link(id="a-1", name="one", module="com.example.one", fqn="com.example.one/one"),
        _link(id="a-2", name="two", module="com.example.two", fqn="com.example.two/two"),
        _link(id="a-3", name="three", module="com.example.two", fqn="com.example.two/three"),
    ]
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/vco/api/actions").respond(200, json={"link": links, "total": 3})
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.action.list",
            target=_Target(),
            params={"module": "com.example.two", "max_results": 1},
        )
    assert result.status == "ok", result.error
    assert route.calls[0].request.headers["accept"] == "application/json"
    assert result.result["items"][0]["id"] == "a-2"
    assert result.result["count"] == 1
    assert result.result["total"] == 2
    assert result.result["more_rows"] is True


@pytest.mark.asyncio
async def test_config_element_list_lists_elements(session: AsyncSession) -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get("/vco/api/configurations").respond(
            200, json={"link": [_link(id="ce-1", name="<element>")], "total": 1}
        )
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.config_element.list",
            target=_Target(),
            params={},
        )
    assert result.status == "ok", result.error
    assert result.result["element"] is None
    assert result.result["items"][0]["id"] == "ce-1"
    assert result.result["more_rows"] is False


@pytest.mark.asyncio
async def test_config_element_attributes_hide_secret_values(session: AsyncSession) -> None:
    await _register()
    element = {
        "id": "ce-1",
        "name": "<element>",
        "version": "0.0.1",
        "category-id": "<folder-id>",
        "attributes": [
            {"name": "host", "type": "string", "value": {"string": {"value": "<host>"}}},
            {"name": "pw", "type": "SecureString", "value": {"string": {"value": "s-1"}}},
            {"name": "key", "type": "string", "encrypt-value": True, "value": "s-2"},
            {"name": "apiToken", "type": "string", "value": {"string": {"value": "s-3"}}},
            {"name": "blob", "type": "EncryptedString", "value": "s-4"},
        ],
    }
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        route = mock.get("/vco/api/configurations/ce-1").respond(200, json=element)
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.config_element.list",
            target=_Target(),
            params={"config_element_id": "ce-1"},
        )
    assert result.status == "ok", result.error
    assert route.called
    assert result.result["element"]["category_id"] == "<folder-id>"
    rows = {row["name"]: row for row in result.result["items"]}
    assert rows["host"]["value"] == {"string": {"value": "<host>"}}
    assert rows["host"]["masked"] is False
    for name in ("pw", "key", "apiToken", "blob"):
        assert rows[name]["masked"] is True
        assert rows[name]["value"] == "***REDACTED***"
    rendered = json.dumps(result.result)
    for secret in ("s-1", "s-2", "s-3", "s-4"):
        assert secret not in rendered


@pytest.mark.asyncio
async def test_config_element_id_must_stay_one_path_segment(session: AsyncSession) -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.config_element.list",
            target=_Target(),
            params={"config_element_id": ".."},
        )
        assert not mock.calls
    assert result.status != "ok"


@pytest.mark.asyncio
async def test_package_list_returns_flat_rows(session: AsyncSession) -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get("/vco/api/packages").respond(
            200, json={"link": [_link(name="com.example.package", version="1.2.0")]}
        )
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.package.list",
            target=_Target(),
            params={},
        )
    assert result.status == "ok", result.error
    assert result.result["items"][0]["name"] == "com.example.package"
    assert result.result["items"][0]["version"] == "1.2.0"


# ---------------------------------------------------------------------------
# Input values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("type_name", "value", "wrapped"),
    [
        ("string", "x", {"string": {"value": "x"}}),
        ("number", 2.5, {"number": {"value": 2.5}}),
        ("boolean", True, {"boolean": {"value": True}}),
        ("Date", "2026-01-01T00:00:00Z", {"date": {"value": "2026-01-01T00:00:00Z"}}),
        ("SecureString", "s", {"secure-string": {"value": "s"}}),
        (
            "Array/string",
            ["a", "b"],
            {"array": {"elements": [{"string": {"value": "a"}}, {"string": {"value": "b"}}]}},
        ),
        ("VC:VirtualMachine", "<id>", {"sdk-object": {"type": "VC:VirtualMachine", "id": "<id>"}}),
    ],
)
def test_values_are_wrapped_the_vendor_way(type_name: str, value: Any, wrapped: Any) -> None:
    assert wrap_value(type_name, value, name="in") == wrapped


@pytest.mark.parametrize(
    ("type_name", "value"),
    [
        ("number", "2"),
        ("number", True),
        ("boolean", "true"),
        ("string", 3),
        ("Array/string", "a"),
        ("Properties", {"k": "v"}),
        ("VC:VirtualMachine", ""),
    ],
)
def test_values_that_do_not_fit_are_refused(type_name: str, value: Any) -> None:
    with pytest.raises(VroRunRefusedError):
        wrap_value(type_name, value, name="in")


# ---------------------------------------------------------------------------
# The run preview
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_card_names_workflow_inputs_and_blast_radius(
    session: AsyncSession,
) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, {"workflow_id": _WF, "inputs": _INPUTS})
        again = await _card(connector, {"workflow_id": _WF, "inputs": list(reversed(_INPUTS))})

    assert card["workflow"]["id"] == _WF
    assert card["workflow"]["version"] == "1.0.0"
    assert len(card["run_sha256"]) == 64
    assert again["run_sha256"] == card["run_sha256"]
    inputs = {item["name"]: item for item in card["inputs"]}
    assert inputs["adminPassword"] == {
        "name": "adminPassword",
        "type": "SecureString",
        "secret_ref": "<vault-path>#password",
    }
    assert inputs["count"]["value"] == 2
    radius = card["blast_radius"]
    assert radius["object"]["kind"] == "vro-workflow"
    assert radius["object"]["id"] == _WF
    assert radius["children"] == [
        {"kind": "VC:VirtualMachine", "id": "<vcenter>/vm-1", "input": "vm"}
    ]
    assert "cannot undo" in radius["irreversibility"]


@pytest.mark.asyncio
async def test_preview_hash_changes_when_the_workflow_changes(session: AsyncSession) -> None:
    connector = await _register()
    params = {"workflow_id": _WF, "inputs": _INPUTS[:1]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        before = await _card(connector, params)
    changed = {**_CONTENT_PAYLOAD, "display-name": "<new-name>"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_WORKFLOW).respond(200, json=_WORKFLOW_PAYLOAD)
        mock.get(_CONTENT).respond(200, json=changed)
        after = await _card(connector, params)
    assert before["run_sha256"] != after["run_sha256"]


@pytest.mark.asyncio
async def test_preview_refuses_a_missing_workflow(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_WORKFLOW).respond(404, json={"message": "not found"})
        with pytest.raises(VroRunRefusedError, match="no workflow"):
            await _card(connector, {"workflow_id": _WF})


@pytest.mark.parametrize(
    ("inputs", "match"),
    [
        ([{"name": "adminPassword", "value": "x"}], "secret"),
        ([{"name": "unknown", "value": "x"}], "no input named"),
        ([{"name": "count", "secret_ref": "<vault-path>#f"}], "only a SecureString"),
        ([{"name": "vmName", "value": "a"}, {"name": "vmName", "value": "b"}], "twice"),
        ([{"name": "count", "value": "two"}], "does not fit"),
    ],
)
@pytest.mark.asyncio
async def test_preview_refuses_wrong_inputs(
    session: AsyncSession, inputs: list[dict[str, Any]], match: str
) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        with pytest.raises(VroRunRefusedError, match=match):
            await _card(connector, {"workflow_id": _WF, "inputs": inputs})


# ---------------------------------------------------------------------------
# The governed flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_is_refused_without_a_matching_preview_hash(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        post = mock.post(_EXECUTIONS)
        for preview_hash in (None, "deadbeef" * 8):
            result = await dispatch(
                operator=_operator(),
                connector_id=VRO_CONNECTOR_ID,
                op_id=RUN_OP_ID,
                target=_Target(),
                params={"workflow_id": _WF, "inputs": _INPUTS},
                preview_hash=preview_hash,
            )
            assert result.status == "denied"
            assert result.extras["error_code"] in (
                "preview_binding_required",
                "preview_hash_mismatch",
            )
    assert not post.called


async def _seed_target() -> None:
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                tenant_id=_TENANT,
                name="vro-run-target",
                aliases=[],
                product=VRO_PRODUCT,
                host=_HOST,
                port=443,
                fqdn=None,
                secret_ref="targets/op-run/vro-run",
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "9.1.1"},
                preferred_impl_id=VRO_IMPL_ID,
                notes="seeded by test_connectors_vro_run_and_library",
            )
        )
        await s.commit()


@pytest.mark.asyncio
async def test_preview_wait_approve_then_the_run_starts_once_with_no_secret_kept(
    _stub_embedding: AsyncMock, _vault: AsyncMock, session: AsyncSession
) -> None:
    await _register()
    await _seed_target()
    requester = _operator("op-run-requester")
    args = {
        "connector_id": VRO_CONNECTOR_ID,
        "op_id": RUN_OP_ID,
        "target": "vro-run-target",
        "params": {"workflow_id": _WF, "inputs": _INPUTS},
    }
    with patch(
        "meho_backplane.connectors.vro.connector.flight_recorder_capture.record_vendor_call"
    ) as record:
        async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
            _mock_workflow(mock)
            _mock_runs(mock, "completed")
            post = mock.post(_EXECUTIONS).respond(
                202,
                headers={"Location": f"https://<orchestrator>{_EXECUTIONS}/{_RUN}/"},
            )
            preview = await preview_operation(requester, args)
            assert preview["status"] == "ok", preview
            bound = {**args, "preview_hash": preview["preview_hash"]}
            call = await call_operation(requester, bound)
            assert call["status"] == "awaiting_approval", call
            assert not post.called
            request_id = UUID(call["extras"]["approval_request_id"])

            approver = _operator("op-run-approver")
            async with get_sessionmaker()() as s:
                approved = await approve_request(s, request_id, operator=approver, params=None)
                await s.commit()
            assert approved.status == "approved"
            async with get_sessionmaker()() as s:
                row = await s.get(ApprovalRequest, request_id)
                assert row is not None
                effect = dict(row.proposed_effect)
                result = await resume_dispatch_after_approval(
                    operator=approver, request=row, params=None
                )

    assert result.status == "ok", result.error
    assert result.result["status"] == "accepted"
    assert result.result["execution_id"] == _RUN
    assert result.result["status_read"] == "vro.execution.get"
    assert post.call_count == 1
    sent = json.loads(post.calls[0].request.content)
    params = {p["name"]: p for p in sent["parameters"]}
    assert params["adminPassword"]["value"] == {"secure-string": {"value": _SECRET}}
    assert params["vm"]["value"] == {
        "sdk-object": {"type": "VC:VirtualMachine", "id": "<vcenter>/vm-1"}
    }
    assert params["tags"]["value"]["array"]["elements"][1] == {"string": {"value": "b"}}
    assert effect["blast_radius"]["object"]["id"] == _WF

    async with get_sessionmaker()() as s:
        audits = (await s.execute(select(AuditLog))).scalars().all()
    kept = [json.dumps(effect, default=str), json.dumps(result.result), str(result.extras)]
    kept += [json.dumps(a.__dict__, default=str) for a in audits]
    kept += [repr(c.kwargs) for c in record.call_args_list]
    for text in kept:
        assert _SECRET not in text
    post_spans = [c for c in record.call_args_list if c.kwargs.get("method") == "POST"]
    assert post_spans
    assert all(c.kwargs["request_body"] is None for c in post_spans)


# ---------------------------------------------------------------------------
# The approved run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stops_when_the_workflow_changed_since_the_approval(
    session: AsyncSession, _vault: AsyncMock
) -> None:
    connector = await _register()
    params = {"workflow_id": _WF, "inputs": _INPUTS}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
    changed = {**_WORKFLOW_PAYLOAD, "version": "1.0.1"}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_WORKFLOW).respond(200, json=changed)
        mock.get(_CONTENT).respond(200, json=_CONTENT_PAYLOAD)
        post = mock.post(_EXECUTIONS)
        answer = await _approved_run(connector, params, card)
    assert answer["status"] == "precondition_failed"
    assert answer["execution_id"] is None
    assert not post.called
    _vault.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_stops_when_the_inputs_differ_from_the_card(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, {"workflow_id": _WF, "inputs": _INPUTS[:1]})
        post = mock.post(_EXECUTIONS)
        other = {"workflow_id": _WF, "inputs": [{"name": "vmName", "value": "<other>"}]}
        answer = await _approved_run(connector, other, card)
    assert answer["status"] == "precondition_failed"
    assert not post.called


@pytest.mark.asyncio
async def test_run_stops_without_an_approval_card(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        post = mock.post(_EXECUTIONS)
        answer = await _approved_run(connector, {"workflow_id": _WF}, None)
        empty = await _approved_run(connector, {"workflow_id": _WF}, {})
    assert answer["status"] == "precondition_failed"
    assert empty["status"] == "precondition_failed"
    assert not post.called


@pytest.mark.asyncio
async def test_run_returns_the_running_run_and_starts_nothing(session: AsyncSession) -> None:
    connector = await _register()
    params = {"workflow_id": _WF}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
        _mock_runs(mock, "completed", "waiting")
        post = mock.post(_EXECUTIONS)
        answer = await _approved_run(connector, params, card)
    assert answer["status"] == "already_running"
    assert answer["execution_id"] == "run-1"
    assert answer["state"] == "waiting"
    assert not post.called


@pytest.mark.asyncio
async def test_run_with_allow_parallel_skips_the_check_and_reads_the_id_from_the_body(
    session: AsyncSession,
) -> None:
    connector = await _register()
    params = {"workflow_id": _WF, "allow_parallel": True}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
        runs = _mock_runs(mock, "running")
        mock.post(_EXECUTIONS).respond(202, json={"id": "run-9", "state": "running"})
        answer = await _approved_run(connector, params, card)
    assert not runs.called
    assert answer["status"] == "accepted"
    assert answer["execution_id"] == "run-9"
    assert answer["state"] == "running"


@pytest.mark.asyncio
async def test_run_vendor_error_text_reaches_the_answer(session: AsyncSession) -> None:
    connector = await _register()
    params = {"workflow_id": _WF}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
        _mock_runs(mock)
        mock.post(_EXECUTIONS).respond(400, json={"message": "<vendor validation error>"})
        with pytest.raises(ConnectorUpstreamError) as raised:
            await _approved_run(connector, params, card)
    assert raised.value.http_status == 400
    assert "<vendor validation error>" in str(raised.value.upstream_message)


@pytest.mark.asyncio
async def test_run_vendor_error_that_echoes_a_secret_is_withheld(
    session: AsyncSession, _vault: AsyncMock
) -> None:
    connector = await _register()
    params = {"workflow_id": _WF, "inputs": _INPUTS[1:2]}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
        _mock_runs(mock)
        mock.post(_EXECUTIONS).respond(400, json={"message": f"bad value {_SECRET}"})
        with pytest.raises(ConnectorUpstreamError) as raised:
            await _approved_run(connector, params, card)
    assert _SECRET not in str(raised.value.upstream_message)
    assert "withheld" in str(raised.value.upstream_message)
    assert raised.value.__context__ is None or _SECRET not in str(raised.value.__context__)


@pytest.mark.asyncio
async def test_status_read_after_the_start_logs_in_again_when_the_login_expired(
    _stub_embedding: AsyncMock, session: AsyncSession
) -> None:
    connector = await _register()
    params = {"workflow_id": _WF, "allow_parallel": True}
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        _mock_workflow(mock)
        card = await _card(connector, params)
        mock.post(_EXECUTIONS).respond(202, json={"id": _RUN})
        started = await _approved_run(connector, params, card)
        state = mock.get(_RUN_PATH)
        state.side_effect = [
            httpx.Response(401),
            httpx.Response(200, json={"id": _RUN, "state": "running"}),
        ]
        _loader.reset_mock()
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id=started["status_read"],
            target=_Target(),
            params={"workflow_id": _WF, "execution_id": started["execution_id"]},
        )
    assert result.status == "ok", result.error
    assert result.result["outcome"] == "running"
    assert state.call_count == 2
    assert _loader.await_count >= 1


# ---------------------------------------------------------------------------
# Cancel a run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_waits_for_approval(_stub_embedding: AsyncMock, session: AsyncSession) -> None:
    await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        delete = mock.delete(_STATE)
        result = await dispatch(
            operator=_operator(),
            connector_id=VRO_CONNECTOR_ID,
            op_id="vro.execution.cancel",
            target=_Target(),
            params={"workflow_id": _WF, "execution_id": _RUN},
        )
    assert result.status == "awaiting_approval", result
    assert not delete.called


@pytest.mark.asyncio
async def test_cancel_sends_the_cancel_for_a_running_run(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_STATE).respond(200, json={"value": "running"})
        delete = mock.delete(_STATE).respond(204)
        answer = await connector.execution_cancel(
            _operator(), _Target(), {"workflow_id": _WF, "execution_id": _RUN}
        )
    assert delete.call_count == 1
    assert answer["status"] == "cancel_requested"
    assert answer["previous_state"] == "running"
    assert answer["status_read"] == "vro.execution.get"


@pytest.mark.asyncio
async def test_cancel_of_an_ended_run_is_unchanged(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_STATE).respond(200, json={"value": "completed"})
        delete = mock.delete(_STATE)
        answer = await connector.execution_cancel(
            _operator(), _Target(), {"workflow_id": _WF, "execution_id": _RUN}
        )
    assert not delete.called
    assert answer["status"] == "unchanged"


@pytest.mark.asyncio
async def test_cancel_vendor_error_reaches_the_caller(session: AsyncSession) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        mock.get(_STATE).respond(200, json={"value": "running"})
        mock.delete(_STATE).respond(400, json={"message": "<vendor cancel error>"})
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await connector.execution_cancel(
                _operator(), _Target(), {"workflow_id": _WF, "execution_id": _RUN}
            )
    assert "<vendor cancel error>" in raised.value.response.text


@pytest.mark.parametrize("bad", ["..", ".", ""])
@pytest.mark.asyncio
async def test_cancel_ids_must_stay_one_path_segment(session: AsyncSession, bad: str) -> None:
    connector = await _register()
    async with respx.mock(base_url=_BASE_URL, assert_all_called=False) as mock:
        with pytest.raises(ValueError):
            await connector.execution_cancel(
                _operator(), _Target(), {"workflow_id": _WF, "execution_id": bad}
            )
        assert not mock.calls
