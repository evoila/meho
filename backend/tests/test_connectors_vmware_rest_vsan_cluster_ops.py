# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vSAN cluster operations of #4446, on a mocked wire.

HCL database update, cluster power pre-check, cluster shut down and start,
proactive tests and object repair. The tests drive the real connector over
respx, on both vSAN transports:

* vCenter 9.1: VI-JSON ``POST /sdk/vim25/9.1.0.0/vsan/<Type>/<moId>/<Method>``;
* vCenter 8.0: SOAP to ``/vsanHealth`` after a vCenter SOAP login.

The vCenter answers are hand-made fixtures in the shape of the vendor types
(``ClusterPowerContext``, ``VsanClusterHealthSummary``, ``TaskInfo``,
``VsanFault``), with made-up names and ids. The sub-call gate is replaced by a
recorder, so a test can let a change through or hold it.
"""

from __future__ import annotations

import base64
import gzip
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from jsonschema import Draft202012Validator

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_checks,
    _vsan_checks_schemas,
    _vsan_common,
    _vsan_hcl,
    _vsan_power,
    _vsan_schemas,
)
from meho_backplane.connectors.vmware_rest.composites._vsan_ops import (
    VSAN_CLUSTER_COMPOSITES,
    VSAN_CLUSTER_GOVERNED_SUBOPS,
)
from meho_backplane.connectors.vmware_rest.soap_vsan import build_vsan_envelope
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.operations._errors import result_connector_error
from meho_backplane.operations._preview import _PREVIEW_BUILDERS, PreviewContext
from meho_backplane.settings import get_settings

_BASE = "https://vc-vsan-ops.test.invalid"
_CLUSTER = "domain-c7"
_TASK = "task-301"
_V91 = "/sdk/vim25/9.1.0.0"
_V80 = "/sdk/vim25/8.0.3.0"
_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"
_FAULT_TEXT = "The vSAN service could not do the action."

_HEALTH = "/vsan/VsanVcClusterHealthSystem/vsan-cluster-health-system"
_POWER = "/vsan/VsanClusterPowerSystem/vsan-cluster-power-system"
_VUM = "/vsan/VsanVumSystem/vsan-vum-system"
_RETRIEVE = "/PropertyCollector/propertyCollector/RetrievePropertiesEx"


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    monkeypatch.setenv("VAULT_OIDC_ROLE", "meho-mcp")
    monkeypatch.setenv("VAULT_OIDC_MOUNT_PATH", "jwt")
    monkeypatch.setenv("VAULT_TIMEOUT_SECONDS", "5.0")
    monkeypatch.delenv("VAULT_NAMESPACE", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Gate:
    """Stands in for ``enforce_subop_policy``: records each call, returns *verdict*."""

    def __init__(self, verdict: OperationResult | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.verdict = verdict

    async def __call__(self, **kwargs: Any) -> OperationResult | None:
        self.calls.append(kwargs)
        return self.verdict


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    recorder = _Gate()
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", recorder)
    return recorder


@dataclass
class _Target:
    name: str = "vc-vsan-ops"
    host: str = "vc-vsan-ops.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-vsan-ops"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _operator() -> Operator:
    return Operator(
        sub="op-vsan-ops",
        name="vSAN Ops Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a1"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


async def _close(connector: VmwareRestConnector) -> None:
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


def _versions(namespace: str, version: str) -> str:
    return (
        '<namespaces version="1.0"><namespace>'
        f"<name>{namespace}</name><version>{version}</version>"
        "</namespace></namespaces>"
    )


def _soap(inner: str) -> str:
    return (
        f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}" xmlns:xsi="{_XSI}">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


def _soap_fault(fault_type: str, text: str) -> str:
    return _soap(
        "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{text}</faultstring>"
        f'<detail><{fault_type}Fault xmlns="urn:vsan" xsi:type="{fault_type}">'
        f"</{fault_type}Fault></detail></soapenv:Fault>"
    )


_SERVICE_CONTENT = _soap(
    '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
    '<propertyCollector type="PropertyCollector">propertyCollector</propertyCollector>'
    '<sessionManager type="SessionManager">SessionManager</sessionManager>'
    "<about><version>8.0.3</version><apiVersion>8.0.3.0</apiVersion>"
    "<apiType>VirtualCenter</apiType></about></returnval></RetrieveServiceContentResponse>"
)
_LOGIN_OK = _soap(
    '<LoginResponse xmlns="urn:vim25"><returnval><key>k</key>'
    "<userName>svc-meho</userName></returnval></LoginResponse>"
)


def _task_info(state: str, *, result: Any = None, error: Any = None) -> dict[str, Any]:
    info: dict[str, Any] = {"_typeName": "TaskInfo", "key": _TASK, "state": state}
    if result is not None:
        info["result"] = result
    if error is not None:
        info["error"] = error
    return info


class _Vcenter:
    """A fake vCenter: VI-JSON vSAN paths, property reads, SOAP and the file access.

    ``vsan`` maps a vSAN path (without the release prefix) to its answer, or
    to a function of the request body. ``objects`` maps ``(type, moid)`` to the
    properties a ``RetrievePropertiesEx`` returns. ``soap`` maps a vSAN method
    name to the ``/vsanHealth`` SOAP answer (vCenter 8.0).
    """

    def __init__(self, release: str) -> None:
        self.release = release
        self.vsan: dict[str, Any] = {}
        self.objects: dict[tuple[str, str], dict[str, Any]] = {}
        self.soap: dict[str, str] = {}
        self.files: dict[str, bytes] = {}
        self.vsan_bodies: list[tuple[str, Any]] = []
        self.soap_bodies: list[str] = []
        self.file_requests: list[httpx.Request] = []

    def mock(self, mock: respx.MockRouter) -> None:
        mock.post("/api/session").respond(200, json="rest-token")
        mock.get("/api/about").respond(404, json={})
        mock.get("/sdk/vimServiceVersions.xml").respond(
            200, text=_versions("urn:vim25", self.release)
        )
        mock.get("/sdk/vsanServiceVersions.xml").respond(200, text=_versions("urn:vsan", "8.0.0.4"))
        mock.post("/sdk").mock(side_effect=self._sdk)
        mock.post("/vsanHealth").mock(side_effect=self._vsan_soap)
        mock.post(path__startswith=f"/sdk/vim25/{self.release}/").mock(side_effect=self._vijson)
        mock.get(path__startswith="/folder/").mock(side_effect=self._file)

    def _sdk(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        if "<RetrieveServiceContent " in body:
            return httpx.Response(200, text=_SERVICE_CONTENT)
        if "<Login " in body:
            return httpx.Response(
                200, text=_LOGIN_OK, headers={"set-cookie": 'vmware_soap_session="c-1"; Path=/'}
            )
        return httpx.Response(500, text="<unexpected/>")

    def _vsan_soap(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        self.soap_bodies.append(body)
        for method, answer in self.soap.items():
            if f"<{method} " in body:
                status = 500 if "Fault>" in answer else 200
                return httpx.Response(status, text=answer)
        return httpx.Response(500, text=_soap_fault("NotSupported", "no fixture"))

    def _vijson(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path[len(f"/sdk/vim25/{self.release}") :]
        body = json.loads(request.content) if request.content else None
        if path == _RETRIEVE:
            return httpx.Response(200, json=self._retrieve(body))
        self.vsan_bodies.append((path, body))
        answer = self.vsan.get(path)
        if answer is None:
            return httpx.Response(500, json={"_typeName": "NotSupported", "faultstring": path})
        if callable(answer):
            answer = answer(body)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def _retrieve(self, body: dict[str, Any]) -> dict[str, Any]:
        objects = []
        for spec in body["specSet"]:
            for obj in spec["objectSet"]:
                ref = obj["obj"]
                props = self.objects.get((ref["type"], ref["value"]))
                if props is None:
                    continue
                objects.append(
                    {
                        "obj": ref,
                        "propSet": [{"name": k, "val": v} for k, v in props.items()],
                    }
                )
        return {"_typeName": "RetrieveResult", "objects": objects}

    def _file(self, request: httpx.Request) -> httpx.Response:
        self.file_requests.append(request)
        content = self.files.get(request.url.path)
        if content is None:
            return httpx.Response(404)
        return httpx.Response(200, content=content)


async def _run(vcenter: _Vcenter, call: Callable[[VmwareRestConnector], Any]) -> Any:
    connector = VmwareRestConnector(session_loader=_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            vcenter.mock(mock)
            return await call(connector)
    finally:
        await _close(connector)


def _call(handler: Any, **params: Any) -> Callable[[VmwareRestConnector], Any]:
    async def _go(connector: VmwareRestConnector) -> Any:
        return await handler(
            operator=_operator(), target=_Target(), params=params, connector=connector
        )

    return _go


def _schema_ok(schema: dict[str, Any], answer: dict[str, Any]) -> None:
    Draft202012Validator(schema).validate(answer)


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    return vim_moref(mo_type, moid)


# ---------------------------------------------------------------------------
# Fixtures in the shape of the vendor answers
# ---------------------------------------------------------------------------


def _precheck_summary(red: bool) -> dict[str, Any]:
    tests = [
        {
            "_typeName": "VsanClusterHealthTest",
            "testId": "com.vmware.vsan.health.test.powerprecheck.ha",
            "testName": "vSphere HA",
            "testShortDescription": "Checks the HA settings.",
            "testHealth": "yellow",
        }
    ]
    if red:
        tests.append(
            {
                "_typeName": "VsanClusterHealthTest",
                "testId": "com.vmware.vsan.health.test.powerprecheck.resync",
                "testName": "Data resync",
                "testShortDescription": "Checks for running resync.",
                "testHealth": "red",
            }
        )
    return {
        "_typeName": "VsanClusterHealthSummary",
        "timestamp": "2026-10-30T10:00:00Z",
        "overallHealth": "red" if red else "yellow",
        "groups": [
            {
                "_typeName": "VsanClusterHealthGroup",
                "groupId": "com.vmware.vsan.health.test.powerprecheck",
                "groupName": "Cluster power",
                "groupHealth": "red" if red else "yellow",
                "groupTests": tests,
            }
        ],
    }


def _power_context(*, current: str = "clusterPoweredOn", task: str | None = None) -> dict:
    context: dict[str, Any] = {
        "_typeName": "ClusterPowerContext",
        "currentClusterPowerStatus": current,
        "orderedClusterPowerStatus": ["clusterPoweredOn"],
    }
    if task is not None:
        context["trackingTask"] = _moref("Task", task)
    return context


def _vc91(**vsan: Any) -> _Vcenter:
    vcenter = _Vcenter("9.1.0.0")
    vcenter.vsan.update(vsan)
    return vcenter


# ---------------------------------------------------------------------------
# Registry rows: risk level and approval of every operation in the table
# ---------------------------------------------------------------------------


_EXPECTED_POLICY = {
    "vmware.composite.vsan.hcl.update": ("caution", True),
    "vmware.vsan.cluster.power.precheck": ("safe", False),
    "vmware.composite.vsan.cluster.shutdown": ("dangerous", True),
    "vmware.composite.vsan.cluster.start": ("dangerous", True),
    "vmware.composite.vsan.proactive_test.run": ("dangerous", True),
    "vmware.composite.vsan.object.repair": ("dangerous", True),
}


def test_every_operation_has_the_risk_level_and_approval_of_the_table() -> None:
    rows = {row["op_id"]: row for row in VSAN_CLUSTER_COMPOSITES}
    assert set(rows) == set(_EXPECTED_POLICY)
    for op_id, (safety, approval) in _EXPECTED_POLICY.items():
        assert rows[op_id]["safety_level"] == safety, op_id
        assert rows[op_id]["requires_approval"] is approval, op_id


def test_every_change_has_a_preview_and_a_sub_call_list() -> None:
    changes = {row["op_id"] for row in VSAN_CLUSTER_COMPOSITES if row["requires_approval"]}
    assert changes == set(VSAN_CLUSTER_GOVERNED_SUBOPS)
    assert changes <= set(_PREVIEW_BUILDERS)


def test_every_example_validates_against_its_schema() -> None:
    for row in VSAN_CLUSTER_COMPOSITES:
        schema = row["parameter_schema"]
        for example in schema["examples"]:
            Draft202012Validator(schema).validate(example)


# ---------------------------------------------------------------------------
# Power pre-check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_precheck_on_vcenter_9_lists_blocking_checks_and_the_power_task() -> None:
    vcenter = _vc91(
        **{
            f"{_HEALTH}/VsanQueryVcClusterHealthSummary": _precheck_summary(red=True),
            f"{_POWER}/QueryClusterPowerContext": _power_context(task="task-9"),
        }
    )
    vcenter.objects[("Task", "task-9")] = {"info": _task_info("success")}
    out = await _run(
        vcenter, _call(_vsan_power.vsan_cluster_power_precheck_composite, cluster=_CLUSTER)
    )

    health_body = dict(vcenter.vsan_bodies)[f"{_HEALTH}/VsanQueryVcClusterHealthSummary"]
    assert health_body["perspective"] == "clusterPowerOffPrecheck"
    assert health_body["cluster"]["value"] == _CLUSTER
    assert out["ready_to_shut_down"] is False
    assert [row["test_name"] for row in out["blocking"]] == ["Data resync"]
    assert [row["test_name"] for row in out["warnings"]] == ["vSphere HA"]
    assert out["power"]["current_status"] == "clusterPoweredOn"
    assert out["power"]["tracking_task"]["state"] == "success"
    _schema_ok(_vsan_schemas.POWER_PRECHECK_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_precheck_on_vcenter_8_goes_over_vsan_soap() -> None:
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["VsanQueryVcClusterHealthSummary"] = _soap(
        '<VsanQueryVcClusterHealthSummaryResponse xmlns="urn:vsan"><returnval>'
        "<groups><groupId>g</groupId><groupName>Cluster power</groupName>"
        "<groupHealth>green</groupHealth><groupTests><testId>t</testId>"
        "<testName>vSphere HA</testName><testHealth>green</testHealth></groupTests>"
        "</groups><overallHealth>green</overallHealth>"
        "<timestamp>2026-10-30T10:00:00Z</timestamp>"
        "</returnval></VsanQueryVcClusterHealthSummaryResponse>"
    )
    vcenter.soap["QueryClusterPowerContext"] = _soap(
        '<QueryClusterPowerContextResponse xmlns="urn:vsan"><returnval>'
        "<currentClusterPowerStatus>clusterPoweredOn</currentClusterPowerStatus>"
        "</returnval></QueryClusterPowerContextResponse>"
    )
    out = await _run(
        vcenter, _call(_vsan_power.vsan_cluster_power_precheck_composite, cluster=_CLUSTER)
    )

    assert vcenter.vsan_bodies == []
    health = next(b for b in vcenter.soap_bodies if "VsanQueryVcClusterHealthSummary" in b)
    assert "<perspective>clusterPowerOffPrecheck</perspective>" in health
    power = next(b for b in vcenter.soap_bodies if "QueryClusterPowerContext" in b)
    assert '<_this type="VsanClusterPowerSystem">vsan-cluster-power-system</_this>' in power
    assert out["ready_to_shut_down"] is True
    assert out["power"]["tracking_task"] is None
    _schema_ok(_vsan_schemas.POWER_PRECHECK_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_precheck_refuses_a_cluster_that_is_not_a_moid() -> None:
    out = await _run(
        _vc91(), _call(_vsan_power.vsan_cluster_power_precheck_composite, cluster="<name>")
    )
    assert out["status"] == "invalid_request"


# ---------------------------------------------------------------------------
# Shut down and start (vendor jobs)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_on_vcenter_9_answers_accepted_with_the_task(gate: _Gate) -> None:
    vcenter = _vc91(
        **{
            f"{_POWER}/QueryClusterPowerContext": _power_context(),
            f"{_POWER}/PerformClusterPowerAction": _moref("Task", _TASK),
        }
    )
    out = await _run(
        vcenter,
        _call(_vsan_power.vsan_cluster_shutdown_composite, cluster=_CLUSTER, reason="power work"),
    )

    body = dict(vcenter.vsan_bodies)[f"{_POWER}/PerformClusterPowerAction"]
    assert body["spec"] == {
        "_typeName": "PerformClusterPowerActionSpec",
        "targetPowerStatus": "clusterPoweredOff",
        "powerOffReason": "power work",
    }
    assert out["status"] == "accepted"
    assert out["task"] == _TASK
    assert out["status_read"] == {"op_id": "vmware.task.info", "params": {"task": _TASK}}
    assert "not done" in out["guidance"]
    assert [c["op_id"] for c in gate.calls] == [
        "POST:/vsan/VsanClusterPowerSystem/{moId}/PerformClusterPowerAction"
    ]
    _schema_ok(_vsan_schemas.POWER_ACTION_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_start_on_vcenter_8_sends_the_spec_as_soap(gate: _Gate) -> None:
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["QueryClusterPowerContext"] = _soap(
        '<QueryClusterPowerContextResponse xmlns="urn:vsan"><returnval>'
        "<currentClusterPowerStatus>clusterPoweredOff</currentClusterPowerStatus>"
        "</returnval></QueryClusterPowerContextResponse>"
    )
    vcenter.soap["PerformClusterPowerAction"] = _soap(
        '<PerformClusterPowerActionResponse xmlns="urn:vsan">'
        f'<returnval type="Task">{_TASK}</returnval></PerformClusterPowerActionResponse>'
    )
    out = await _run(vcenter, _call(_vsan_power.vsan_cluster_start_composite, cluster=_CLUSTER))

    action = next(b for b in vcenter.soap_bodies if "<PerformClusterPowerAction " in b)
    assert (
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
        "<spec>"
        "<targetPowerStatus>clusterPoweredOn</targetPowerStatus></spec>"
    ) in action
    assert out["status"] == "accepted"
    assert out["task"] == _TASK
    assert out["current_status"] == "clusterPoweredOff"


@pytest.mark.asyncio
async def test_shutdown_waits_for_approval_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vsan/VsanClusterPowerSystem/{moId}/PerformClusterPowerAction",
        result=None,
        duration_ms=1.0,
    )
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", _Gate(held))
    vcenter = _vc91(**{f"{_POWER}/QueryClusterPowerContext": _power_context()})
    out = await _run(vcenter, _call(_vsan_power.vsan_cluster_shutdown_composite, cluster=_CLUSTER))

    assert out is held
    assert [path for path, _ in vcenter.vsan_bodies] == [f"{_POWER}/QueryClusterPowerContext"]


@pytest.mark.asyncio
async def test_shutdown_returns_the_running_task_and_starts_nothing(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_POWER}/QueryClusterPowerContext": _power_context(task="task-55")})
    vcenter.objects[("Task", "task-55")] = {"info": _task_info("running")}
    out = await _run(vcenter, _call(_vsan_power.vsan_cluster_shutdown_composite, cluster=_CLUSTER))

    assert out["status"] == "already_running"
    assert out["task"] == "task-55"
    assert out["status_read"]["params"] == {"task": "task-55"}
    assert gate.calls == []
    _schema_ok(_vsan_schemas.POWER_ACTION_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_start_of_a_running_cluster_is_unchanged(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_POWER}/QueryClusterPowerContext": _power_context()})
    out = await _run(vcenter, _call(_vsan_power.vsan_cluster_start_composite, cluster=_CLUSTER))
    assert out["status"] == "unchanged"
    assert gate.calls == []


@pytest.mark.asyncio
async def test_shutdown_vendor_fault_reaches_the_answer(gate: _Gate) -> None:
    fault = {"_typeName": "VsanFault", "faultstring": _FAULT_TEXT}
    vcenter = _vc91(
        **{
            f"{_POWER}/QueryClusterPowerContext": _power_context(),
            f"{_POWER}/PerformClusterPowerAction": httpx.Response(500, json=fault),
        }
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        await _run(vcenter, _call(_vsan_power.vsan_cluster_shutdown_composite, cluster=_CLUSTER))
    result = result_connector_error("vmware.composite.vsan.cluster.shutdown", raised.value, 1.0)
    assert result.extras["upstream_fault"] == {"type": "VsanFault", "faultstring": _FAULT_TEXT}


@pytest.mark.asyncio
async def test_start_vendor_fault_on_vcenter_8_reaches_the_answer(gate: _Gate) -> None:
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["QueryClusterPowerContext"] = _soap(
        '<QueryClusterPowerContextResponse xmlns="urn:vsan"><returnval>'
        "<currentClusterPowerStatus>clusterPoweredOff</currentClusterPowerStatus>"
        "</returnval></QueryClusterPowerContextResponse>"
    )
    vcenter.soap["PerformClusterPowerAction"] = _soap_fault("VsanFault", _FAULT_TEXT)
    with pytest.raises(RuntimeError) as raised:
        await _run(vcenter, _call(_vsan_power.vsan_cluster_start_composite, cluster=_CLUSTER))
    result = result_connector_error("vmware.composite.vsan.cluster.start", raised.value, 1.0)
    assert result.extras["upstream_fault"] == {"type": "VsanFault", "faultstring": _FAULT_TEXT}


@pytest.mark.asyncio
async def test_shutdown_preview_shows_the_precheck_and_what_else_is_affected() -> None:
    vcenter = _vc91(
        **{
            f"{_HEALTH}/VsanQueryVcClusterHealthSummary": _precheck_summary(red=True),
            f"{_POWER}/QueryClusterPowerContext": _power_context(),
        }
    )

    async def _preview(connector: VmwareRestConnector) -> Any:
        ctx = PreviewContext(
            descriptor=_Descriptor("vmware.composite.vsan.cluster.shutdown"),  # type: ignore[arg-type]
            connector_instance=connector,
            operator=_operator(),
            target=_Target(),
            params={"cluster": _CLUSTER},
        )
        return await _vsan_power.cluster_power_preview(ctx)

    out = await _run(vcenter, _preview)
    assert out["precheck"]["ready_to_shut_down"] is False
    assert "stops" in out["blast_radius"]["affected"]


@dataclass
class _Descriptor:
    op_id: str


# ---------------------------------------------------------------------------
# HCL database and release catalog
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hcl_update_from_the_web_sends_the_url(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_HEALTH}/VsanVcUpdateHclDbFromWeb": True})
    out = await _run(
        vcenter,
        _call(
            _vsan_hcl.vsan_hcl_update_composite,
            hcl={"source": "web", "url": "https://hcl.example.invalid/all.json"},
        ),
    )
    assert vcenter.vsan_bodies == [
        (f"{_HEALTH}/VsanVcUpdateHclDbFromWeb", {"url": "https://hcl.example.invalid/all.json"})
    ]
    assert out["status"] == "updated"
    assert out["steps_ran"][0]["step"] == "hcl"
    _schema_ok(_vsan_schemas.HCL_UPDATE_RESPONSE_SCHEMA, out)


def _datastore_objects(vcenter: _Vcenter) -> None:
    vcenter.objects[("Datastore", "datastore-15")] = {
        "name": "ds-shared",
        "summary.type": "VMFS",
        "browser": _moref("HostDatastoreBrowser", "datastoreBrowser-datastore-15"),
        "parent": _moref("Folder", "group-s5"),
        "vm": [],
    }
    vcenter.objects[("Folder", "group-s5")] = {
        "name": "datastore",
        "parent": _moref("Datacenter", "datacenter-3"),
    }
    vcenter.objects[("Datacenter", "datacenter-3")] = {
        "name": "dc-a",
        "parent": _moref("Folder", "group-d10"),
    }
    vcenter.objects[("Folder", "group-d10")] = {
        "name": "region-a",
        "parent": _moref("Folder", "group-d1"),
    }
    vcenter.objects[("Folder", "group-d1")] = {"name": "Datacenters"}


@pytest.mark.asyncio
async def test_hcl_update_from_a_datastore_file_gzips_and_reads_through_vcenter(
    gate: _Gate,
) -> None:
    hcl = json.dumps({"data": {"controller": []}, "timestamp": 1}).encode()
    vcenter = _vc91(**{f"{_HEALTH}/VsanVcUploadHclDb": True})
    _datastore_objects(vcenter)
    vcenter.files["/folder/vsan/all.json"] = hcl
    out = await _run(
        vcenter,
        _call(
            _vsan_hcl.vsan_hcl_update_composite,
            hcl={"source": "datastore_file", "datastore": "datastore-15", "path": "vsan/all.json"},
        ),
    )

    request = vcenter.file_requests[0]
    assert request.url.params["dsName"] == "ds-shared"
    assert request.url.params["dcPath"] == "region-a/dc-a"
    assert request.headers["Cookie"] == 'vmware_soap_session="c-1"'
    path, body = vcenter.vsan_bodies[0]
    assert path == f"{_HEALTH}/VsanVcUploadHclDb"
    assert gzip.decompress(base64.b64decode(body["db"])) == hcl
    assert out["status"] == "updated"
    assert out["steps_ran"][0]["file"] == "[ds-shared] vsan/all.json"
    assert out["steps_ran"][0]["size_bytes"] == len(hcl)


@pytest.mark.asyncio
async def test_hcl_update_fails_at_step_two_and_lists_what_ran(gate: _Gate) -> None:
    fault = {"_typeName": "VsanFault", "faultstring": _FAULT_TEXT}
    vcenter = _vc91(
        **{
            f"{_HEALTH}/VsanVcUpdateHclDbFromWeb": True,
            f"{_VUM}/VsanVcUploadReleaseDb": httpx.Response(500, json=fault),
        }
    )
    _datastore_objects(vcenter)
    vcenter.files["/folder/vsan/release.json"] = b'{"releases": []}'
    out = await _run(
        vcenter,
        _call(
            _vsan_hcl.vsan_hcl_update_composite,
            hcl={"source": "web"},
            release_catalog={"datastore": "datastore-15", "path": "vsan/release.json"},
        ),
    )

    assert out["status"] == "partly_done"
    assert [row["step"] for row in out["steps_ran"]] == ["hcl"]
    assert out["failed_step"] == {
        "step": "release_catalog",
        "type": "VsanFault",
        "message": _FAULT_TEXT,
    }
    assert out["steps_not_run"] == []
    assert out["issues"][0]["severity"] == "error"
    assert dict(vcenter.vsan_bodies)[f"{_VUM}/VsanVcUploadReleaseDb"] == {"db": '{"releases": []}'}
    _schema_ok(_vsan_schemas.HCL_UPDATE_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_hcl_update_with_a_missing_file_runs_nothing(gate: _Gate) -> None:
    vcenter = _vc91()
    _datastore_objects(vcenter)
    out = await _run(
        vcenter,
        _call(
            _vsan_hcl.vsan_hcl_update_composite,
            hcl={"source": "datastore_file", "datastore": "datastore-15", "path": "vsan/x.json"},
            release_catalog={"datastore": "datastore-15", "path": "vsan/release.json"},
        ),
    )
    assert out["status"] == "failed"
    assert out["steps_ran"] == []
    assert out["failed_step"]["step"] == "hcl"
    assert "not found" in out["failed_step"]["message"]
    assert out["steps_not_run"] == ["release_catalog"]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_hcl_update_waits_for_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(status="awaiting_approval", op_id="x", result=None, duration_ms=1.0)
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", _Gate(held))
    vcenter = _vc91()
    out = await _run(vcenter, _call(_vsan_hcl.vsan_hcl_update_composite, hcl={"source": "web"}))
    assert out is held
    assert vcenter.vsan_bodies == []


@pytest.mark.asyncio
async def test_hcl_update_on_vcenter_8_sends_the_url_as_soap(gate: _Gate) -> None:
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["VsanVcUpdateHclDbFromWeb"] = _soap(
        '<VsanVcUpdateHclDbFromWebResponse xmlns="urn:vsan">'
        "<returnval>true</returnval></VsanVcUpdateHclDbFromWebResponse>"
    )
    out = await _run(
        vcenter,
        _call(_vsan_hcl.vsan_hcl_update_composite, hcl={"source": "web", "url": "https://a/b?c&d"}),
    )
    assert "<url>https://a/b?c&amp;d</url>" in vcenter.soap_bodies[0]
    assert out["status"] == "updated"


def test_hcl_and_release_arguments() -> None:
    plain = b'{"a": 1}'
    assert gzip.decompress(base64.b64decode(_vsan_hcl.hcl_db_argument(plain))) == plain
    zipped = gzip.compress(plain)
    assert base64.b64decode(_vsan_hcl.hcl_db_argument(zipped)) == zipped
    assert _vsan_hcl.release_db_argument(zipped) == '{"a": 1}'
    with pytest.raises(_vsan_hcl.DatastoreFileError):
        _vsan_hcl.hcl_db_argument(b"not json")


@pytest.mark.asyncio
async def test_hcl_update_refuses_an_empty_request(gate: _Gate) -> None:
    out = await _run(_vc91(), _call(_vsan_hcl.vsan_hcl_update_composite))
    assert out["status"] == "invalid_request"
    _schema_ok(_vsan_schemas.HCL_UPDATE_RESPONSE_SCHEMA, out)


# ---------------------------------------------------------------------------
# Proactive tests
# ---------------------------------------------------------------------------


_CLUSTER_RESULT = {
    "_typeName": "VsanClusterProactiveTestResult",
    "overallStatus": "green",
    "overallStatusDescription": "All hosts passed.",
    "timestamp": "2026-10-30T11:00:00Z",
}


@pytest.mark.asyncio
async def test_vm_creation_test_returns_one_row_per_host(gate: _Gate) -> None:
    result = {
        "_typeName": "VsanClusterCreateVmHealthTestResult",
        "clusterResult": _CLUSTER_RESULT,
        "hostResults": [
            {"hostname": "esx-a.test.invalid", "state": "Passed"},
            {
                "hostname": "esx-b.test.invalid",
                "state": "Failed",
                "fault": {"_typeName": "VsanFault", "faultstring": "no space"},
            },
        ],
    }
    vcenter = _vc91(**{f"{_HEALTH}/VsanQueryVcClusterCreateVmHealthTest": result})
    out = await _run(
        vcenter,
        _call(_vsan_checks.vsan_proactive_test_composite, cluster=_CLUSTER, test="vm_creation"),
    )
    _path, body = vcenter.vsan_bodies[0]
    assert body["timeout"] == 20
    assert out["status"] == "completed"
    assert out["overall_status"] == "green"
    assert [(r["host"], r["status"]) for r in out["hosts"]] == [
        ("esx-a.test.invalid", "Passed"),
        ("esx-b.test.invalid", "Failed"),
    ]
    assert "no space" in out["hosts"][1]["error"]
    _schema_ok(_vsan_checks_schemas.PROACTIVE_TEST_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_network_test_on_vcenter_8_sends_whole_numbers_as_soap(gate: _Gate) -> None:
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["VsanQueryVcClusterNetworkPerfTest"] = _soap(
        '<VsanQueryVcClusterNetworkPerfTestResponse xmlns="urn:vsan"><returnval>'
        "<clusterResult><overallStatus>green</overallStatus>"
        "<overallStatusDescription>ok</overallStatusDescription>"
        "<timestamp>2026-10-30T11:00:00Z</timestamp></clusterResult>"
        "<hostResults><hostname>esx-a.test.invalid</hostname><status>green</status>"
        "<client>true</client><bandwidthBps>9000000000</bandwidthBps>"
        "<totalBytes>1</totalBytes><lossPct>0</lossPct><jitterMs>0.5</jitterMs>"
        "</hostResults></returnval></VsanQueryVcClusterNetworkPerfTestResponse>"
    )
    out = await _run(
        vcenter,
        _call(
            _vsan_checks.vsan_proactive_test_composite,
            cluster=_CLUSTER,
            test="network_performance",
            duration_seconds=10,
        ),
    )
    assert "<multicast>false</multicast><durationSec>10</durationSec>" in vcenter.soap_bodies[0]
    assert out["hosts"][0]["bandwidth_bps"] == 9000000000
    assert out["hosts"][0]["jitter_ms"] == 0.5
    _schema_ok(_vsan_checks_schemas.PROACTIVE_TEST_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_short_test_refuses_a_time_above_25_seconds(gate: _Gate) -> None:
    out = await _run(
        _vc91(),
        _call(
            _vsan_checks.vsan_proactive_test_composite,
            cluster=_CLUSTER,
            test="vm_creation",
            duration_seconds=120,
        ),
    )
    assert out["status"] == "invalid_request"
    assert gate.calls == []


_WORKLOADS = [
    {
        "_typeName": "VsanStorageWorkloadType",
        "typeId": "basic-test",
        "name": "Basic test",
        "description": "Short write and read load.",
        "specs": [
            {
                "_typeName": "VsanVmdkLoadTestSpec",
                "vmdkIOSpec": {"_typeName": "VsanVmdkIOLoadSpec", "readPct": 70},
            }
        ],
    }
]


@pytest.mark.asyncio
async def test_storage_test_with_an_unknown_workload_lists_the_known_ones(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_HEALTH}/VsanQueryVcClusterVmdkWorkloadTypes": _WORKLOADS})
    out = await _run(
        vcenter,
        _call(
            _vsan_checks.vsan_proactive_test_composite,
            cluster=_CLUSTER,
            test="storage_load",
            workload="nope",
        ),
    )
    assert out["status"] == "invalid_request"
    assert out["workloads"] == ["basic-test"]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_storage_test_on_vcenter_8_reads_every_workload_type(gate: _Gate) -> None:
    """SOAP sends a list answer as one returnval per entry; MEHO reads all of them."""
    vcenter = _Vcenter("8.0.3.0")
    vcenter.soap["VsanQueryVcClusterVmdkWorkloadTypes"] = _soap(
        '<VsanQueryVcClusterVmdkWorkloadTypesResponse xmlns="urn:vsan">'
        "<returnval><typeId>basic-test</typeId><name>Basic</name></returnval>"
        "<returnval><typeId>stress-test</typeId><name>Stress</name></returnval>"
        "</VsanQueryVcClusterVmdkWorkloadTypesResponse>"
    )
    out = await _run(
        vcenter,
        _call(
            _vsan_checks.vsan_proactive_test_composite,
            cluster=_CLUSTER,
            test="storage_load",
            workload="nope",
        ),
    )
    assert out["status"] == "invalid_request"
    assert out["workloads"] == ["basic-test", "stress-test"]
    assert gate.calls == []


@pytest.mark.asyncio
async def test_storage_test_follows_the_task_and_returns_its_result(gate: _Gate) -> None:
    vcenter = _vc91(
        **{
            f"{_HEALTH}/VsanQueryVcClusterVmdkWorkloadTypes": _WORKLOADS,
            f"{_HEALTH}/VsanVcClusterRunVmdkLoadTest": _moref("Task", _TASK),
        }
    )
    vcenter.objects[("Task", _TASK)] = {
        "info": _task_info(
            "success",
            result={
                "_typeName": "VsanClusterVmdkLoadTestResult",
                "clusterResult": _CLUSTER_RESULT,
                "hostResults": [
                    {
                        "hostname": "esx-a.test.invalid",
                        "issueFound": False,
                        "vmdkResults": [{"success": True, "iops": 5000, "avgLatencyUs": 900}],
                    }
                ],
            },
        )
    }
    out = await _run(
        vcenter,
        _call(
            _vsan_checks.vsan_proactive_test_composite,
            cluster=_CLUSTER,
            test="storage_load",
            workload="basic-test",
            duration_seconds=60,
        ),
    )
    body = dict(vcenter.vsan_bodies)[f"{_HEALTH}/VsanVcClusterRunVmdkLoadTest"]
    assert body["durationSec"] == 60
    assert body["specs"] == _WORKLOADS[0]["specs"]
    assert out["status"] == "completed"
    assert out["task"] == _TASK
    assert out["hosts"][0]["iops"] == 5000
    _schema_ok(_vsan_checks_schemas.PROACTIVE_TEST_RESPONSE_SCHEMA, out)


# ---------------------------------------------------------------------------
# Object repair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_object_repair_follows_the_task_to_its_end(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_HEALTH}/VsanHealthRepairClusterObjectsImmediate": _moref("Task", _TASK)})
    vcenter.objects[("Task", _TASK)] = {"info": _task_info("success")}
    out = await _run(
        vcenter,
        _call(_vsan_checks.vsan_object_repair_composite, cluster=_CLUSTER, object_uuids=["u-1"]),
    )
    body = dict(vcenter.vsan_bodies)[f"{_HEALTH}/VsanHealthRepairClusterObjectsImmediate"]
    assert body["uuids"] == ["u-1"]
    assert out["status"] == "repair_queued"
    _schema_ok(_vsan_checks_schemas.OBJECT_REPAIR_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_object_repair_task_fault_reaches_the_answer(gate: _Gate) -> None:
    vcenter = _vc91(**{f"{_HEALTH}/VsanHealthRepairClusterObjectsImmediate": _moref("Task", _TASK)})
    vcenter.objects[("Task", _TASK)] = {
        "info": _task_info(
            "error",
            error={"_typeName": "VsanFault", "faultstring": _FAULT_TEXT},
        )
    }
    out = await _run(vcenter, _call(_vsan_checks.vsan_object_repair_composite, cluster=_CLUSTER))
    assert out["status"] == "failed"
    assert _FAULT_TEXT in out["error"]
    assert out["issues"][0]["severity"] == "error"
    _schema_ok(_vsan_checks_schemas.OBJECT_REPAIR_RESPONSE_SCHEMA, out)


@pytest.mark.asyncio
async def test_object_repair_waits_for_approval(monkeypatch: pytest.MonkeyPatch) -> None:
    held = OperationResult(status="awaiting_approval", op_id="x", result=None, duration_ms=1.0)
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", _Gate(held))
    vcenter = _vc91()
    out = await _run(vcenter, _call(_vsan_checks.vsan_object_repair_composite, cluster=_CLUSTER))
    assert out is held
    assert vcenter.vsan_bodies == []


# ---------------------------------------------------------------------------
# The vSAN SOAP codec: the value types these operations send
# ---------------------------------------------------------------------------


def test_build_vsan_envelope_writes_numbers_texts_lists_and_data_objects() -> None:
    envelope = build_vsan_envelope(
        "VsanVcClusterHealthSystem",
        "vsan-cluster-health-system",
        "VsanVcClusterRunVmdkLoadTest",
        {
            "cluster": vim_moref("ClusterComputeResource", _CLUSTER),
            "runname": "a<b",
            "durationSec": 60,
            "specs": [
                {"_typeName": "VsanVmdkLoadTestSpec", "stepDurationSec": 5, "skip": None},
                {"vmdkIOSpec": {"readPct": 70}},
            ],
        },
    )
    assert (
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
        "<runname>a&lt;b</runname><durationSec>60</durationSec>"
        "<specs><stepDurationSec>5</stepDurationSec></specs>"
        "<specs><vmdkIOSpec><readPct>70</readPct></vmdkIOSpec></specs>"
    ) in envelope
