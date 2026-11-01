# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the VM-template library changes: deploy, check out, check in.

A REST fake stands in for the connector session. It answers the
``vm-template/library-items`` calls and the read-backs with saved vSphere REST
shapes (made-up ids and names). No live vCenter. Two layers:

* the handlers, with the policy check of the write seam replaced by a
  recorder: the exact request bodies, the read after the change, the vendor
  faults, and a held write that never reaches vCenter;
* the governed flow through the real dispatcher on both catalogs: each change
  waits for a second person and then runs once; an agent without a grant is
  refused.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, PrincipalKind, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.registry import clear_registry, register_connector_v2
from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import _vm_template as tpl
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.composites._governed_subops import (
    _GOVERNED_SUBOP_MANIFEST,
)
from meho_backplane.connectors.vmware_rest.composites._register import (
    _COMPOSITES,
    COMPOSITES_NOT_ON_8_0,
    register_vmware_composite_operations,
    register_vmware_composite_operations_8_0,
)
from meho_backplane.connectors.vmware_rest.connector import VmwareRest80Connector
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import ApprovalRequest, EndpointDescriptor
from meho_backplane.db.models import Target as TargetORM
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.operations._handler_resolve import _CONNECTOR_INSTANCE_CACHE
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.operations._validate import validate_params
from meho_backplane.operations.approval_queue import (
    approve_request,
    resume_dispatch_after_approval,
)
from meho_backplane.operations.meta_tools import call_operation
from meho_backplane.settings import get_settings

_ITEM = "00000000-0000-4000-8000-0000000000aa"
_NEW_VM = "vm-1001"
_CHECKED_OUT_VM = "vm-1002"
_ITEM_PATH = f"/api/vcenter/vm-template/library-items/{_ITEM}"
_DEPLOY = f"{_ITEM_PATH}?action=deploy"
_CHECK_OUT = f"{_ITEM_PATH}/check-outs?action=check-out"
_CHECK_IN = f"{_ITEM_PATH}/check-outs/{_CHECKED_OUT_VM}?action=check-in"
_VERSION = "3"

_VM_INFO = {
    "name": "<vm-name>",
    "power_state": "POWERED_ON",
    "guest_OS": "OTHER_LINUX_64",
    "cpu": {"count": 2, "cores_per_socket": 1},
    "memory": {"size_MiB": 4096},
}
_TEMPLATE_INFO = {
    "vm_template": "vm-2001",
    "guest_OS": "OTHER_LINUX_64",
    "cpu": {"count": 4, "cores_per_socket": 2},
    "memory": {"size_mib": 8192},
    "disks": {"2000": {"capacity": 1}},
    "nics": {"4000": {"network": "network-1"}, "4001": {"network": "network-2"}},
    "vm_home_storage": {"datastore": "datastore-1"},
}


def _operator() -> Operator:
    return Operator(
        sub="op-vm-template",
        name="VM Template Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a344"),
        tenant_role=TenantRole.OPERATOR,
    )


class _Target:
    name = "vc-test"


def _status_error(path: str, status: int, error_type: str, text: str) -> httpx.HTTPError:
    request = httpx.Request("POST", f"https://vc.test{path}")
    body = {"error_type": error_type, "messages": [{"default_message": text}]}
    response = httpx.Response(status, json=body, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


class _RestFake:
    """Recording double for the connector's REST session."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.gets: dict[str, Any] = {
            f"/api/vcenter/vm/{_NEW_VM}": _VM_INFO,
            f"/api/vcenter/vm/{_CHECKED_OUT_VM}": {**_VM_INFO, "power_state": "POWERED_OFF"},
            f"{_ITEM_PATH}/check-outs/{_CHECKED_OUT_VM}": {
                "time": "2026-10-01T08:00:00.000Z",
                "user": "<user>",
            },
            _ITEM_PATH: _TEMPLATE_INFO,
        }
        self.post_results: dict[str, Any] = {
            _DEPLOY: _NEW_VM,
            _CHECK_OUT: _CHECKED_OUT_VM,
            _CHECK_IN: _VERSION,
        }
        self.post_errors: dict[str, httpx.HTTPError] = {}
        self.get_errors: dict[str, httpx.HTTPError] = {}
        self.posts: list[tuple[str, Any, Any]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Operator) -> str:
        return f"/api{path}"

    async def adapt_op_query(self, target: Any, query: Any, operator: Operator) -> Any:
        return query

    async def _get_json(
        self, target: Any, path: str, *, operator: Operator, params: Any = None
    ) -> Any:
        if path in self.get_errors:
            raise self.get_errors[path]
        if path not in self.gets:
            raise AssertionError(f"unexpected GET {path!r}")
        return copy.deepcopy(self.gets[path])

    async def _post_json(
        self,
        target: Any,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        json: Any = None,
        timeout: Any = None,
    ) -> Any:
        assert verb == "POST"
        self.posts.append((path, copy.deepcopy(json), timeout))
        if path in self.post_errors:
            raise self.post_errors[path]
        return copy.deepcopy(self.post_results[path])


class _GateRecorder:
    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self._verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self._verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _GateRecorder:
    recorder = _GateRecorder()
    monkeypatch.setattr(_write, "enforce_subop_policy", recorder)
    return recorder


async def _run(handler: Any, conn: _RestFake, **params: Any) -> Any:
    return await handler(
        operator=_operator(),
        target=_Target(),
        params=params,
        connector=conn,  # type: ignore[arg-type]
    )


def _spec(op_id: str) -> Any:
    return next(s for s in _COMPOSITES if s.op_id == op_id)


_OPS = [tpl.OP_ID_DEPLOY, tpl.OP_ID_CHECK_OUT, tpl.OP_ID_CHECK_IN]


# ---------------------------------------------------------------------------
# Registration and contracts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_id", _OPS)
def test_each_change_is_dangerous_and_waits_for_approval(op_id: str) -> None:
    spec = _spec(op_id)
    assert spec.safety_level == "dangerous"
    assert spec.requires_approval is True
    assert spec.group_key == "content_library"
    assert op_id not in COMPOSITES_NOT_ON_8_0
    assert op_id in _GOVERNED_SUBOP_MANIFEST
    for key in ("when_to_call", "preconditions", "output_shape", "next_step"):
        assert spec.llm_instructions[key]


@pytest.mark.parametrize("op_id", _OPS)
def test_examples_are_valid_and_use_placeholders(op_id: str) -> None:
    schema = _spec(op_id).parameter_schema
    for example in schema["examples"]:
        validate_params(schema, example)
        assert "templates" not in str(example)


@pytest.mark.parametrize(
    ("params", "valid"),
    [
        ({"template_library_item": _ITEM, "name": "<vm-name>", "cluster": "domain-c1"}, True),
        ({"template_library_item": _ITEM, "name": "<vm-name>", "host": "host-1"}, True),
        ({"template_library_item": _ITEM, "name": "<vm-name>"}, False),
        ({"template_library_item": "my-template", "name": "x", "host": "host-1"}, False),
        ({"template_library_item": _ITEM, "name": "x", "host": "host-1", "spec": {}}, False),
    ],
)
def test_deploy_parameter_schema(params: dict[str, Any], valid: bool) -> None:
    errors = list(Draft202012Validator(tpl.DEPLOY_PARAMETER_SCHEMA).iter_errors(params))
    assert (not errors) is valid


# ---------------------------------------------------------------------------
# deploy
# ---------------------------------------------------------------------------


async def test_deploy_sends_the_flat_deploy_spec_and_reads_the_vm_back(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake()
    out = await _run(
        tpl.vm_template_deploy_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        description="<notes>",
        cluster="domain-c1",
        folder="group-v1",
        datastore="datastore-1",
        powered_on=True,
        customization_spec_name="<spec-name>",
    )
    assert conn.posts == [
        (
            _DEPLOY,
            {
                "name": "<vm-name>",
                "description": "<notes>",
                "placement": {"folder": "group-v1", "cluster": "domain-c1"},
                "vm_home_storage": {"datastore": "datastore-1"},
                "disk_storage": {"datastore": "datastore-1"},
                "powered_on": True,
                "guest_customization": {"name": "<spec-name>"},
            },
            _write._LIBRARY_ITEM_DEPLOY_TIMEOUT,
        )
    ]
    assert out["status"] == "deployed"
    assert out["vm"] == _NEW_VM
    assert out["vm_info"] == {
        "name": "<vm-name>",
        "power_state": "POWERED_ON",
        "guest_os": "OTHER_LINUX_64",
        "cpu_count": 2,
        "memory_mib": 4096,
    }
    Draft202012Validator(tpl.DEPLOY_RESPONSE_SCHEMA).validate(out)
    assert gate.calls[0]["op_id"] == _write._OP_DEPLOY_LIBRARY_VM
    assert gate.calls[0]["params"]["templateLibraryItem"] == _ITEM


async def test_deploy_minimal_body(gate: _GateRecorder) -> None:
    conn = _RestFake()
    await _run(
        tpl.vm_template_deploy_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        host="host-1",
    )
    assert conn.posts[0][1] == {
        "name": "<vm-name>",
        "placement": {"host": "host-1"},
        "powered_on": False,
    }


async def test_deploy_keeps_the_vm_when_the_read_back_fails(gate: _GateRecorder) -> None:
    conn = _RestFake()
    path = f"/api/vcenter/vm/{_NEW_VM}"
    conn.get_errors[path] = _status_error(path, 503, "SERVICE_UNAVAILABLE", "busy")
    out = await _run(
        tpl.vm_template_deploy_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        host="host-1",
    )
    assert out["status"] == "deployed"
    assert out["vm"] == _NEW_VM
    assert out["vm_info"] is None
    assert "SERVICE_UNAVAILABLE" in out["read_note"]


@pytest.mark.parametrize(
    ("status", "error_type", "word"),
    [(400, "ALREADY_EXISTS", "refused"), (404, "NOT_FOUND", "not_found")],
)
async def test_deploy_vendor_fault_becomes_a_structured_answer(
    gate: _GateRecorder, status: int, error_type: str, word: str
) -> None:
    conn = _RestFake()
    conn.post_errors[_DEPLOY] = _status_error(_DEPLOY, status, error_type, "the vendor text")
    out = await _run(
        tpl.vm_template_deploy_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        host="host-1",
    )
    assert out["status"] == word
    assert out["error_type"] == error_type
    assert "the vendor text" in out["guidance"]
    assert out["vm"] is None
    Draft202012Validator(tpl.DEPLOY_RESPONSE_SCHEMA).validate(out)


async def test_deploy_server_error_raises(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_errors[_DEPLOY] = _status_error(_DEPLOY, 500, "ERROR", "boom")
    with pytest.raises(httpx.HTTPStatusError):
        await _run(
            tpl.vm_template_deploy_composite,
            conn,
            template_library_item=_ITEM,
            name="<vm-name>",
            host="host-1",
        )


async def test_a_held_write_never_reaches_vcenter(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id=_write._OP_DEPLOY_LIBRARY_VM,
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_write, "enforce_subop_policy", _GateRecorder(held))
    conn = _RestFake()
    out = await _run(
        tpl.vm_template_deploy_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        host="host-1",
    )
    assert out is held
    assert conn.posts == []


async def test_a_template_id_that_is_not_a_uuid_is_refused_before_any_call(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake()
    with pytest.raises(ValueError, match="UUID"):
        await _run(
            tpl.vm_template_deploy_composite,
            conn,
            template_library_item="../../vm",
            name="<vm-name>",
            host="host-1",
        )
    assert conn.posts == []
    assert gate.calls == []


# ---------------------------------------------------------------------------
# check out
# ---------------------------------------------------------------------------


async def test_check_out_sends_the_check_out_spec_and_reads_back(gate: _GateRecorder) -> None:
    conn = _RestFake()
    out = await _run(
        tpl.vm_template_check_out_composite,
        conn,
        template_library_item=_ITEM,
        name="<vm-name>",
        resource_pool="resgroup-1",
    )
    assert conn.posts == [
        (
            _CHECK_OUT,
            {
                "name": "<vm-name>",
                "powered_on": False,
                "placement": {"resource_pool": "resgroup-1"},
            },
            httpx.USE_CLIENT_DEFAULT,
        )
    ]
    assert out["status"] == "checked_out"
    assert out["vm"] == _CHECKED_OUT_VM
    assert out["check_out"] == {"time": "2026-10-01T08:00:00.000Z", "user": "<user>"}
    assert out["vm_info"]["power_state"] == "POWERED_OFF"
    assert out["read_note"] is None
    Draft202012Validator(tpl.CHECK_OUT_RESPONSE_SCHEMA).validate(out)


async def test_check_out_without_placement_sends_no_placement(gate: _GateRecorder) -> None:
    conn = _RestFake()
    await _run(
        tpl.vm_template_check_out_composite, conn, template_library_item=_ITEM, name="<vm-name>"
    )
    assert conn.posts[0][1] == {"name": "<vm-name>", "powered_on": False}


async def test_check_out_of_a_subscribed_item_is_refused(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_errors[_CHECK_OUT] = _status_error(
        _CHECK_OUT, 400, "INVALID_ARGUMENT", "item is in a subscribed library"
    )
    out = await _run(
        tpl.vm_template_check_out_composite, conn, template_library_item=_ITEM, name="<vm-name>"
    )
    assert out["status"] == "refused"
    assert "subscribed library" in out["guidance"]
    Draft202012Validator(tpl.CHECK_OUT_RESPONSE_SCHEMA).validate(out)


# ---------------------------------------------------------------------------
# check in
# ---------------------------------------------------------------------------


async def test_check_in_sends_the_message_and_reads_the_template_back(
    gate: _GateRecorder,
) -> None:
    conn = _RestFake()
    out = await _run(
        tpl.vm_template_check_in_composite,
        conn,
        template_library_item=_ITEM,
        vm=_CHECKED_OUT_VM,
        message="<what changed>",
    )
    assert conn.posts == [(_CHECK_IN, {"message": "<what changed>"}, httpx.USE_CLIENT_DEFAULT)]
    assert out["status"] == "checked_in"
    assert out["version"] == _VERSION
    assert out["template"] == {
        "vm_template": "vm-2001",
        "guest_os": "OTHER_LINUX_64",
        "cpu_count": 4,
        "cores_per_socket": 2,
        "memory_mib": 8192,
        "disk_count": 1,
        "nic_count": 2,
    }
    Draft202012Validator(tpl.CHECK_IN_RESPONSE_SCHEMA).validate(out)


async def test_check_in_of_a_running_vm_is_refused(gate: _GateRecorder) -> None:
    conn = _RestFake()
    conn.post_errors[_CHECK_IN] = _status_error(
        _CHECK_IN, 400, "NOT_ALLOWED_IN_CURRENT_STATE", "the virtual machine is powered on"
    )
    out = await _run(
        tpl.vm_template_check_in_composite,
        conn,
        template_library_item=_ITEM,
        vm=_CHECKED_OUT_VM,
        message="<what changed>",
    )
    assert out["status"] == "refused"
    assert out["error_type"] == "NOT_ALLOWED_IN_CURRENT_STATE"
    assert out["template"] is None


async def test_check_in_refuses_a_vm_that_is_not_a_moid(gate: _GateRecorder) -> None:
    conn = _RestFake()
    with pytest.raises(ValueError, match="VM id"):
        await _run(
            tpl.vm_template_check_in_composite,
            conn,
            template_library_item=_ITEM,
            vm="my vm",
            message="<what changed>",
        )
    assert conn.posts == []


# ---------------------------------------------------------------------------
# Approval previews
# ---------------------------------------------------------------------------


class _Descriptor:
    def __init__(self, op_id: str) -> None:
        self.op_id = op_id


def _ctx(op_id: str, conn: _RestFake | None, **params: Any) -> PreviewContext:
    return PreviewContext(
        descriptor=_Descriptor(op_id),  # type: ignore[arg-type]
        connector_instance=conn,  # type: ignore[arg-type]
        operator=_operator(),
        target=_Target(),
        params=params,
    )


@pytest.mark.parametrize("op_id", _OPS)
def test_each_change_registers_a_preview_builder(op_id: str) -> None:
    assert _PREVIEW_BUILDERS[op_id] is tpl._preview


async def test_deploy_preview_shows_the_inputs_and_the_template() -> None:
    preview = await tpl._preview(
        _ctx(
            tpl.OP_ID_DEPLOY,
            _RestFake(),
            template_library_item=_ITEM,
            name="<vm-name>",
            cluster="domain-c1",
            powered_on=True,
        )
    )
    assert preview is not None
    assert preview["summary"] == f"Deploy a new VM '<vm-name>' from VM template item {_ITEM}."
    assert preview["cluster"] == "domain-c1"
    assert preview["powered_on"] is True
    assert preview["template"]["vm_template"] == "vm-2001"
    assert preview["read_note"] is None


async def test_check_in_preview_shows_the_check_out_and_the_vm_state() -> None:
    preview = await tpl._preview(
        _ctx(
            tpl.OP_ID_CHECK_IN,
            _RestFake(),
            template_library_item=_ITEM,
            vm=_CHECKED_OUT_VM,
            message="<what changed>",
        )
    )
    assert preview is not None
    assert preview["check_out"] == {"time": "2026-10-01T08:00:00.000Z", "user": "<user>"}
    assert preview["vm_info"]["power_state"] == "POWERED_OFF"
    assert preview["template"]["cpu_count"] == 4


async def test_preview_without_a_connector_shows_the_inputs_only() -> None:
    preview = await tpl._preview(
        _ctx(tpl.OP_ID_CHECK_OUT, None, template_library_item=_ITEM, name="<vm-name>")
    )
    assert preview == {
        "summary": f"Check out VM template item {_ITEM} as a new VM '<vm-name>'.",
        "template_library_item": _ITEM,
        "name": "<vm-name>",
    }


async def test_preview_reports_a_failed_read() -> None:
    conn = _RestFake()
    conn.get_errors[_ITEM_PATH] = _status_error(_ITEM_PATH, 404, "NOT_FOUND", "no such item")
    preview = await tpl._preview(
        _ctx(tpl.OP_ID_CHECK_OUT, conn, template_library_item=_ITEM, name="<vm-name>")
    )
    assert preview is not None
    assert preview["template"] is None
    assert "no such item" in preview["read_note"]


async def test_preview_declines_a_template_id_that_is_not_a_uuid() -> None:
    preview = await tpl._preview(
        _ctx(tpl.OP_ID_DEPLOY, _RestFake(), template_library_item="x", name="<vm-name>")
    )
    assert preview is None


# ---------------------------------------------------------------------------
# Governed flow through the dispatcher (both catalogs)
# ---------------------------------------------------------------------------

_TENANT_ID = UUID("00000000-0000-0000-0000-000000004344")
_TARGET_NAME = "example-vcenter"


@pytest.fixture
def _dispatch_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    clear_registry()
    yield
    reset_dispatcher_caches()
    clear_registry()
    get_settings.cache_clear()


def _embedding_stub() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


def _person(sub: str, kind: PrincipalKind = PrincipalKind.USER) -> Operator:
    return Operator(
        sub=sub,
        name="VM Template Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=_TENANT_ID,
        tenant_role=TenantRole.OPERATOR,
        principal_kind=kind,
    )


async def _bootstrap(conn: _RestFake, version: str) -> str:
    """Register one catalog, seed a target of that version; return the connector id."""
    if version == "8.0":
        register_connector_v2(
            product="vmware", version="8.0", impl_id="vmware-rest", cls=VmwareRest80Connector
        )
        _CONNECTOR_INSTANCE_CACHE[VmwareRest80Connector] = conn  # type: ignore[assignment]
        await register_vmware_composite_operations_8_0(embedding_service=_embedding_stub())
    else:
        register_connector_v2(
            product="vmware", version="9.0", impl_id="vmware-rest", cls=VmwareRestConnector
        )
        _CONNECTOR_INSTANCE_CACHE[VmwareRestConnector] = conn  # type: ignore[assignment]
        await register_vmware_composite_operations(embedding_service=_embedding_stub())
    async with get_sessionmaker()() as s:
        s.add(
            TargetORM(
                id=uuid.uuid4(),
                tenant_id=_TENANT_ID,
                name=_TARGET_NAME,
                aliases=[],
                product="vmware",
                host="vcenter.example.test",
                port=443,
                fqdn=None,
                secret_ref=None,
                auth_model="shared_service_account",
                vpn_required=False,
                extras={},
                fingerprint={"version": "8.0.3" if version == "8.0" else "9.0"},
                preferred_impl_id="vmware-rest",
                notes="seeded by test_connectors_vmware_rest_vm_template",
            )
        )
        await s.commit()
    return f"vmware-rest-{version}"


async def _approve_and_resume(request_id: UUID) -> Any:
    approver = _person("op-approver")
    async with get_sessionmaker()() as s:
        approved = await approve_request(s, request_id, operator=approver, params=None)
        await s.commit()
    assert approved.status == "approved"
    async with get_sessionmaker()() as s:
        row = await s.get(ApprovalRequest, request_id)
        assert row is not None
        return await resume_dispatch_after_approval(operator=approver, request=row, params=None)


@pytest.mark.usefixtures("_dispatch_env")
@pytest.mark.parametrize("version", ["8.0", "9.0"])
async def test_deploy_waits_for_a_second_person_then_runs_once(version: str) -> None:
    conn = _RestFake()
    connector_id = await _bootstrap(conn, version)
    async with get_sessionmaker()() as s:
        row = (
            await s.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.op_id == tpl.OP_ID_DEPLOY,
                    EndpointDescriptor.version == version,
                )
            )
        ).scalar_one()
    assert row.safety_level == "dangerous"
    assert row.requires_approval is True

    call = await call_operation(
        _person("op-requester"),
        {
            "connector_id": connector_id,
            "op_id": tpl.OP_ID_DEPLOY,
            "target": _TARGET_NAME,
            "params": {"template_library_item": _ITEM, "name": "<vm-name>", "host": "host-1"},
        },
    )
    assert call["status"] == "awaiting_approval", call
    assert conn.posts == []
    request_id = UUID(call["extras"]["approval_request_id"])
    async with get_sessionmaker()() as s:
        approval = await s.get(ApprovalRequest, request_id)
        assert approval is not None
        effect = dict(approval.proposed_effect)
    assert effect["safety_level"] == "dangerous"
    assert effect["preview"]["template"]["vm_template"] == "vm-2001"

    resume = await _approve_and_resume(request_id)
    assert resume.status == "ok", resume.error
    assert resume.result["status"] == "deployed"
    assert [path for path, _, _ in conn.posts] == [_DEPLOY]


@pytest.mark.usefixtures("_dispatch_env")
async def test_an_agent_without_a_grant_is_refused() -> None:
    conn = _RestFake()
    await _bootstrap(conn, "9.0")

    class _FakeTarget:
        product = "vmware"
        fingerprint = type("_Fingerprint", (), {"version": "9.0"})()
        preferred_impl_id = "vmware-rest"
        id = uuid.uuid4()
        tenant_id = _TENANT_ID
        name = _TARGET_NAME
        host = "vcenter.example.test"
        port = 443
        auth_model = "shared_service_account"

    result = await dispatch(
        operator=_person("agent-1", PrincipalKind.AGENT),
        connector_id="vmware-rest-9.0",
        op_id=tpl.OP_ID_CHECK_IN,
        target=_FakeTarget(),
        params={"template_library_item": _ITEM, "vm": _CHECKED_OUT_VM, "message": "<m>"},
    )
    assert result.status == "denied", result
    assert conn.posts == []
