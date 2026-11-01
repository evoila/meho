# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vSAN transport of the vCenter connector, on a mocked wire (#4441).

``vmware.host.vsan_health`` sent its VI-JSON call without the ``/vsan`` part
of the path. vCenter 9.1 answered HTTP 500 ``ManagedObjectNotFound`` for the
vSAN health object, and the read hid that behind empty health fields. These
tests drive the real connector over respx:

* vCenter 9.x: VI-JSON ``POST /sdk/vim25/{release}/vsan/VsanVcClusterHealthSystem/
  vsan-cluster-health-system/VsanQueryVcClusterHealthSummary`` (the path of the
  vendor VI/JSON files of 9.0, 9.1.0 and 9.1.1).
* vCenter 8.0: the vendor VI/JSON files of 8.0 have no vSAN paths, so the
  method goes as SOAP to ``/vsanHealth`` with the cookie of a vCenter SOAP
  login and ``SOAPAction: urn:vsan/<version>`` (vendor sample
  ``vsanapiutils.py`` of the vSAN Management SDK for Python 8.0).

The vCenter answers are hand-made fixtures in the shape of the vendor types
(``VsanClusterHealthSummary``, ``VsanFault``, ``ManagedObjectNotFound``).

#4922 adds the stored result: ``from_cache: true`` sends ``fetchFromCache``
on both transports, and a fresh check that ends with a read timeout is
followed by one cached read.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.base import ConnectorResourceNotFoundError
from meho_backplane.connectors.schemas import AuthModel
from meho_backplane.connectors.vmware_rest import VmwareRestConnector, VsphereTargetLike
from meho_backplane.connectors.vmware_rest._mount import (
    vmomi_mounted_path,
    vsan_method_path,
    vsan_needs_soap,
)
from meho_backplane.connectors.vmware_rest.soap_vsan import (
    build_vsan_envelope,
    parse_fault_object,
    vsan_soap_action,
)
from meho_backplane.connectors.vmware_rest.vim_body import vim_moref
from meho_backplane.operations._errors import (
    result_connector_error,
    result_connector_not_found,
    result_connector_timeout,
)
from meho_backplane.settings import get_settings

_OP_ID = "vmware.host.vsan_health"
_BASE = "https://vc-vsan.test.invalid"
_CLUSTER = "domain-c7"
_CHECKED_AT = "2026-10-25T18:30:12.345Z"
_CLUSTER_BODY = {
    "cluster": {
        "_typeName": "ManagedObjectReference",
        "type": "ClusterComputeResource",
        "value": _CLUSTER,
    }
}
_TIMEOUT_NOTE_LEAD = (
    "The fresh vSAN check did not finish within 30 seconds. "
    "A host of the cluster may be down or in maintenance mode."
)
_SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"
_XSI = "http://www.w3.org/2001/XMLSchema-instance"

#: The vSAN health method on vCenter 9.1, as MEHO must send it.
_VIJSON_91_URL = (
    "/sdk/vim25/9.1.0.0/vsan/VsanVcClusterHealthSystem/vsan-cluster-health-system/"
    "VsanQueryVcClusterHealthSummary"
)
#: The path MEHO sent before #4441, without ``/vsan``.
_OLD_VIJSON_91_URL = (
    "/sdk/vim25/9.1.0.0/VsanVcClusterHealthSystem/vsan-cluster-health-system/"
    "VsanQueryVcClusterHealthSummary"
)


# ---------------------------------------------------------------------------
# Hand-made vCenter answers
# ---------------------------------------------------------------------------

#: A ``VsanClusterHealthSummary`` as the VI-JSON API sends it (9.x).
_VIJSON_SUMMARY: dict[str, Any] = {
    "_typeName": "VsanClusterHealthSummary",
    "timestamp": _CHECKED_AT,
    "overallHealth": "red",
    "overallHealthDescription": "vSAN object health",
    "groups": [
        {
            "_typeName": "VsanClusterHealthGroup",
            "groupId": "com.vmware.vsan.health.test.network",
            "groupName": "Network",
            "groupHealth": "green",
            "groupTests": [
                {
                    "_typeName": "VsanClusterHealthTest",
                    "testId": "com.vmware.vsan.health.test.clusterpartition",
                    "testName": "vSAN cluster partition",
                    "testShortDescription": "Checks if the vSAN cluster is partitioned.",
                    "testHealth": "green",
                },
                {
                    "_typeName": "VsanClusterHealthTest",
                    "testId": "com.vmware.vsan.health.test.vsanvmknic",
                    "testName": "All hosts have a vSAN vmknic configured",
                    "testShortDescription": "Checks the vSAN network adapters.",
                    "testHealth": "green",
                },
            ],
        },
        {
            "_typeName": "VsanClusterHealthGroup",
            "groupId": "com.vmware.vsan.health.test.data",
            "groupName": "Data",
            "groupHealth": "red",
            "groupTests": [
                {
                    "_typeName": "VsanClusterHealthTest",
                    "testId": "com.vmware.vsan.health.test.objecthealth",
                    "testName": "vSAN object health",
                    "testShortDescription": "Checks the health of vSAN objects.",
                    "testHealth": "red",
                }
            ],
        },
    ],
}

#: What ``vmware.host.vsan_health`` returns for that summary.
_EXPECTED_GROUPS: list[dict[str, Any]] = [
    {
        "group_id": "com.vmware.vsan.health.test.network",
        "group_name": "Network",
        "group_health": "green",
        "tests": [
            {
                "test_id": "com.vmware.vsan.health.test.clusterpartition",
                "test_name": "vSAN cluster partition",
                "test_health": "green",
                "test_short_description": "Checks if the vSAN cluster is partitioned.",
            },
            {
                "test_id": "com.vmware.vsan.health.test.vsanvmknic",
                "test_name": "All hosts have a vSAN vmknic configured",
                "test_health": "green",
                "test_short_description": "Checks the vSAN network adapters.",
            },
        ],
    },
    {
        "group_id": "com.vmware.vsan.health.test.data",
        "group_name": "Data",
        "group_health": "red",
        "tests": [
            {
                "test_id": "com.vmware.vsan.health.test.objecthealth",
                "test_name": "vSAN object health",
                "test_health": "red",
                "test_short_description": "Checks the health of vSAN objects.",
            }
        ],
    },
]


def _soap_envelope(inner: str) -> str:
    return (
        f'<soapenv:Envelope xmlns:soapenv="{_SOAP_ENV}" xmlns:xsi="{_XSI}">'
        f"<soapenv:Body>{inner}</soapenv:Body></soapenv:Envelope>"
    )


def _soap_test(test_id: str, name: str, health: str, short: str) -> str:
    return (
        f"<groupTests><testId>{test_id}</testId><testName>{name}</testName>"
        f"<testShortDescription>{short}</testShortDescription>"
        f"<testHealth>{health}</testHealth></groupTests>"
    )


#: The same summary as a SOAP answer of ``/vsanHealth`` (8.0). The second
#: group has one test, which SOAP sends without a list marker.
_SOAP_SUMMARY_XML = _soap_envelope(
    '<VsanQueryVcClusterHealthSummaryResponse xmlns="urn:vsan"><returnval>'
    "<groups><groupId>com.vmware.vsan.health.test.network</groupId>"
    "<groupName>Network</groupName><groupHealth>green</groupHealth>"
    + _soap_test(
        "com.vmware.vsan.health.test.clusterpartition",
        "vSAN cluster partition",
        "green",
        "Checks if the vSAN cluster is partitioned.",
    )
    + _soap_test(
        "com.vmware.vsan.health.test.vsanvmknic",
        "All hosts have a vSAN vmknic configured",
        "green",
        "Checks the vSAN network adapters.",
    )
    + "</groups>"
    "<groups><groupId>com.vmware.vsan.health.test.data</groupId>"
    "<groupName>Data</groupName><groupHealth>red</groupHealth>"
    + _soap_test(
        "com.vmware.vsan.health.test.objecthealth",
        "vSAN object health",
        "red",
        "Checks the health of vSAN objects.",
    )
    + "</groups>"
    "<overallHealth>red</overallHealth>"
    "<overallHealthDescription>vSAN object health</overallHealthDescription>"
    f"<timestamp>{_CHECKED_AT}</timestamp>"
    "</returnval></VsanQueryVcClusterHealthSummaryResponse>"
)

#: What ``vmware.host.vsan_health`` returns for a fresh read of that summary.
_EXPECTED_FRESH_ANSWER: dict[str, Any] = {
    "cluster": _CLUSTER,
    "overall_health": "red",
    "groups": _EXPECTED_GROUPS,
    "checked_at": _CHECKED_AT,
    "from_cache": False,
}

_VSAN_FAULT_TEXT = "The vSAN health check could not run on the cluster."


def _soap_fault(fault_type: str, faultstring: str, detail: str = "") -> str:
    return _soap_envelope(
        "<soapenv:Fault><faultcode>ServerFaultCode</faultcode>"
        f"<faultstring>{faultstring}</faultstring>"
        f'<detail><{fault_type}Fault xmlns="urn:vsan" xsi:type="{fault_type}">'
        f"{detail}</{fault_type}Fault></detail></soapenv:Fault>"
    )


def _not_found_text(vim_type: str, moid: str) -> str:
    return (
        f"The object 'vim.{vim_type}:{moid}' has already been deleted "
        "or has not been completely created"
    )


def _vijson_not_found(mo_type: str, moid: str, vim_type: str | None = None) -> dict[str, Any]:
    """A VI-JSON ``ManagedObjectNotFound`` fault body naming *mo_type*:*moid*."""
    return {
        "_typeName": "ManagedObjectNotFound",
        "faultstring": _not_found_text(vim_type or mo_type, moid),
        "obj": vim_moref(mo_type, moid),
    }


#: The fault vCenter 9.1 gave for the old path (live check on #4441).
_LIVE_HEALTH_SYSTEM_NOT_FOUND = _vijson_not_found(
    "VsanVcClusterHealthSystem",
    "vsan-cluster-health-system",
    vim_type="cluster.VsanVcClusterHealthSystem",
)


def _service_versions(namespace: str, version: str) -> str:
    return (
        '<namespaces version="1.0"><namespace>'
        f"<name>{namespace}</name><version>{version}</version>"
        "</namespace></namespaces>"
    )


_VIM_SERVICE_CONTENT_XML = _soap_envelope(
    '<RetrieveServiceContentResponse xmlns="urn:vim25"><returnval>'
    '<propertyCollector type="PropertyCollector">propertyCollector</propertyCollector>'
    '<sessionManager type="SessionManager">SessionManager</sessionManager>'
    "<about><version>8.0.3</version><apiVersion>8.0.3.0</apiVersion>"
    "<apiType>VirtualCenter</apiType></about>"
    "</returnval></RetrieveServiceContentResponse>"
)
_VIM_LOGIN_OK_XML = _soap_envelope(
    '<LoginResponse xmlns="urn:vim25"><returnval><key>vc-session-key</key>'
    "<userName>svc-meho</userName></returnval></LoginResponse>"
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pin the chassis env vars ``Settings`` reads at construction time."""
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


@dataclass
class _StubTarget:
    name: str = "vc-vsan"
    host: str = "vc-vsan.test.invalid"
    port: int | None = 443
    secret_ref: str = "vsphere/vc-vsan"
    auth_model: str | None = AuthModel.SHARED_SERVICE_ACCOUNT.value
    tls_server_name: str | None = None
    fingerprint: dict[str, Any] | None = None
    id: UUID = field(default_factory=uuid4)
    tenant_id: UUID = field(default_factory=lambda: UUID(int=0))


def _make_operator() -> Operator:
    return Operator(
        sub="op-vsan-transport",
        name="vSAN Transport Test",
        email=None,
        raw_jwt="<test-raw-jwt>",
        tenant_id=UUID("00000000-0000-0000-0000-00000000a0a0"),
        tenant_role=TenantRole.OPERATOR,
    )


async def _stub_loader(_target: VsphereTargetLike, _operator: Operator) -> dict[str, str]:
    return {"username": "svc-meho", "password": "stub-password"}


async def _close(connector: VmwareRestConnector) -> None:
    """Close the pooled clients without the session-revoke call."""
    for client in connector._clients.values():
        await client.aclose()
    connector._clients.clear()


def _mock_vcenter_session(mock: respx.MockRouter, vim_version: str) -> None:
    """The REST session and version probe of a vCenter (it serves no ``/api/about``)."""
    mock.post("/api/session").respond(200, json="rest-token")
    mock.get("/api/about").respond(404, json={})
    mock.get("/sdk/vimServiceVersions.xml").respond(
        200, text=_service_versions("urn:vim25", vim_version)
    )


async def _vsan_health(
    connector: VmwareRestConnector, target: _StubTarget, **params: Any
) -> dict[str, Any]:
    return await connector.host_vsan_health(
        _make_operator(), target, {"cluster": _CLUSTER, **params}
    )


def _read_timeout_then(*answers: httpx.Response) -> Any:
    """A respx side effect: the first call times out on read, the next ones answer."""
    calls: list[httpx.Request] = []

    def _effect(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            raise httpx.ReadTimeout("read timed out", request=request)
        return answers[min(len(calls) - 2, len(answers) - 1)]

    return _effect


# ---------------------------------------------------------------------------
# The shared path helper and the version rule
# ---------------------------------------------------------------------------


def test_vsan_method_path_pins_two_methods() -> None:
    """One helper builds every vSAN path: ``/vsan/<Type>/<moId>/<Method>``."""
    health = vsan_method_path(
        "VsanVcClusterHealthSystem",
        "vsan-cluster-health-system",
        "VsanQueryVcClusterHealthSummary",
    )
    config = vsan_method_path(
        "VsanVcClusterConfigSystem", "vsan-cluster-config-system", "VsanClusterGetConfig"
    )
    assert health == (
        "/vsan/VsanVcClusterHealthSystem/vsan-cluster-health-system/VsanQueryVcClusterHealthSummary"
    )
    assert config == (
        "/vsan/VsanVcClusterConfigSystem/vsan-cluster-config-system/VsanClusterGetConfig"
    )
    # Mounted on the release-qualified VI-JSON base, like any other vmomi method.
    assert vmomi_mounted_path("9.1.0.0", health) == _VIJSON_91_URL
    # With the placeholder it is the path as the vendor VI/JSON file writes it.
    assert (
        vsan_method_path("VsanVcClusterHealthSystem", "{moId}", "VsanQueryVcClusterHealthSummary")
        == "/vsan/VsanVcClusterHealthSystem/{moId}/VsanQueryVcClusterHealthSummary"
    )


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("8.0.3.0", True),
        ("8.0.1.0", True),
        ("8.0.3", True),
        ("7.0.3.0", True),
        ("9.0.0.0", False),
        ("9.1.0.0", False),
        ("9.1.1.0", False),
        # Unknown version: stay on VI-JSON, the path of current vCenter releases.
        (None, False),
        ("", False),
        ("garbage", False),
    ],
)
def test_vsan_needs_soap_only_below_vcenter_9(version: str | None, expected: bool) -> None:
    assert vsan_needs_soap(version) is expected


# ---------------------------------------------------------------------------
# vCenter 9.x: VI-JSON with the /vsan part
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_vcenter_9_reads_health_on_the_vsan_vi_json_path() -> None:
    """vCenter 9.1: the call goes to ``/sdk/vim25/9.1.0.0/vsan/...`` and returns real groups."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            vsan = mock.post(_VIJSON_91_URL).respond(200, json=_VIJSON_SUMMARY)
            old_path = mock.post(_OLD_VIJSON_91_URL).respond(
                500, json=_LIVE_HEALTH_SYSTEM_NOT_FOUND
            )
            soap = mock.post("/vsanHealth").respond(500, text="<unexpected/>")
            result = await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert vsan.call_count == 1
    assert not old_path.called
    assert not soap.called
    assert json.loads(vsan.calls[0].request.content) == _CLUSTER_BODY
    assert result == _EXPECTED_FRESH_ANSWER


@pytest.mark.asyncio
async def test_vcenter_9_from_cache_sends_fetch_from_cache() -> None:
    """#4922: ``from_cache: true`` puts ``fetchFromCache: true`` in the VI-JSON body."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            vsan = mock.post(_VIJSON_91_URL).respond(200, json=_VIJSON_SUMMARY)
            result = await _vsan_health(connector, _StubTarget(), from_cache=True)
    finally:
        await _close(connector)

    assert vsan.call_count == 1
    assert json.loads(vsan.calls[0].request.content) == {**_CLUSTER_BODY, "fetchFromCache": True}
    assert result == {**_EXPECTED_FRESH_ANSWER, "from_cache": True}


@pytest.mark.asyncio
async def test_vcenter_9_fresh_check_timeout_falls_back_to_the_stored_result() -> None:
    """#4922: a read timeout is followed by exactly one call with ``fetchFromCache: true``."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            vsan = mock.post(_VIJSON_91_URL).mock(
                side_effect=_read_timeout_then(httpx.Response(200, json=_VIJSON_SUMMARY))
            )
            result = await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert vsan.call_count == 2
    assert json.loads(vsan.calls[0].request.content) == _CLUSTER_BODY
    assert json.loads(vsan.calls[1].request.content) == {**_CLUSTER_BODY, "fetchFromCache": True}
    assert result == {
        **_EXPECTED_FRESH_ANSWER,
        "from_cache": True,
        "fresh_check": "timed_out",
        "note": f"{_TIMEOUT_NOTE_LEAD} This is the last stored result.",
    }


@pytest.mark.asyncio
async def test_vcenter_9_stored_result_failing_too_is_a_timeout_with_the_plain_text() -> None:
    """#4922: when the cached read fails as well, the answer is ``connector_timeout``, explained."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    fault = {"_typeName": "VsanFault", "faultstring": _VSAN_FAULT_TEXT}
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            vsan = mock.post(_VIJSON_91_URL).mock(
                side_effect=_read_timeout_then(httpx.Response(500, json=fault))
            )
            with pytest.raises(httpx.ReadTimeout) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert vsan.call_count == 2
    result = result_connector_timeout(_OP_ID, raised.value, 1.0)
    assert result.status == "error"
    assert result.error == "connector_timeout: ReadTimeout"
    assert result.extras["error_code"] == "connector_timeout"
    assert result.extras["phase"] == "read"
    assert result.extras["configured_timeout"] == 30.0
    assert result.extras["exception_message"] == (
        f"{_TIMEOUT_NOTE_LEAD} The last stored result could not be read either."
    )


@pytest.mark.asyncio
async def test_vcenter_9_vsan_fault_is_an_error_with_the_vendor_text() -> None:
    """A VI-JSON vSAN fault raises; the error answer carries the fault type and text."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    fault = {"_typeName": "VsanFault", "faultstring": _VSAN_FAULT_TEXT}
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            mock.post(_VIJSON_91_URL).respond(500, json=fault)
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    result = result_connector_error(_OP_ID, raised.value, 1.0)
    assert result.status == "error"
    assert result.extras["http_status"] == 500
    assert result.extras["upstream_fault"] == {"type": "VsanFault", "faultstring": _VSAN_FAULT_TEXT}


@pytest.mark.asyncio
async def test_vcenter_9_missing_cluster_is_not_found() -> None:
    """``ManagedObjectNotFound`` for the cluster the caller sent becomes MEHO's not_found."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            mock.post(_VIJSON_91_URL).respond(
                500, json=_vijson_not_found("ClusterComputeResource", _CLUSTER)
            )
            with pytest.raises(ConnectorResourceNotFoundError) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert raised.value.resource_ids == [_CLUSTER]
    result = result_connector_not_found(_OP_ID, raised.value, 1.0)
    assert result.status == "not_found"
    assert result.extras["resource_id"] == _CLUSTER


@pytest.mark.asyncio
async def test_vcenter_9_missing_vsan_object_stays_an_error() -> None:
    """The fault seen live before #4441 names the vSAN object, not the cluster: no not_found."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            mock.post(_VIJSON_91_URL).respond(500, json=_LIVE_HEALTH_SYSTEM_NOT_FOUND)
            with pytest.raises(httpx.HTTPStatusError) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    result = result_connector_error(_OP_ID, raised.value, 1.0)
    assert result.extras["upstream_fault"]["type"] == "ManagedObjectNotFound"
    assert "vsan-cluster-health-system" in result.extras["upstream_fault"]["faultstring"]


# ---------------------------------------------------------------------------
# vCenter 8.0: SOAP to /vsanHealth
# ---------------------------------------------------------------------------


class _Vcenter80:
    """Replays the SOAP side of a vCenter 8.0: the ``/sdk`` login and ``/vsanHealth``.

    Each Login sets a new cookie (``vsan-cookie-1``, ``-2``, ...).
    ``vsan_answers`` are the ``/vsanHealth`` answers in order (the last one
    repeats). Every ``/vsanHealth`` request is kept for the asserts.
    """

    def __init__(self, vsan_answers: list[httpx.Response]) -> None:
        self.sdk_methods: list[str] = []
        self.vsan_requests: list[httpx.Request] = []
        self._vsan_answers = vsan_answers
        self._logins = 0

    def sdk(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8")
        if "<RetrieveServiceContent " in body:
            self.sdk_methods.append("RetrieveServiceContent")
            return httpx.Response(200, text=_VIM_SERVICE_CONTENT_XML)
        if "<Login " in body:
            self.sdk_methods.append("Login")
            self._logins += 1
            return httpx.Response(
                200,
                text=_VIM_LOGIN_OK_XML,
                headers={
                    "set-cookie": (
                        f'vmware_soap_session="vsan-cookie-{self._logins}"; '
                        "Path=/; HttpOnly; Secure"
                    )
                },
            )
        return httpx.Response(500, text="<unexpected-sdk/>")

    def vsan(self, request: httpx.Request) -> httpx.Response:
        self.vsan_requests.append(request)
        index = min(len(self.vsan_requests), len(self._vsan_answers)) - 1
        return self._vsan_answers[index]


def _mock_vcenter_80(
    mock: respx.MockRouter, router: _Vcenter80, *, vsan_version: str | None = "8.0.0.4"
) -> respx.Route:
    """Mock a vCenter 8.0.3; return the route of ``/sdk/vsanServiceVersions.xml``."""
    _mock_vcenter_session(mock, "8.0.3.0")
    mock.post("/sdk").mock(side_effect=router.sdk)
    mock.post("/vsanHealth").mock(side_effect=router.vsan)
    if vsan_version is None:
        return mock.get("/sdk/vsanServiceVersions.xml").respond(404)
    return mock.get("/sdk/vsanServiceVersions.xml").respond(
        200, text=_service_versions("urn:vsan", vsan_version)
    )


@pytest.mark.asyncio
async def test_vcenter_8_reads_health_over_vsan_soap() -> None:
    """vCenter 8.0: SOAP to ``/vsanHealth``, no VI-JSON call; real groups come back."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80([httpx.Response(200, text=_SOAP_SUMMARY_XML)])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            vijson = mock.post(path__startswith="/sdk/vim25/").respond(500)
            result = await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert not vijson.called
    assert router.sdk_methods == ["RetrieveServiceContent", "Login"]
    assert len(router.vsan_requests) == 1
    request = router.vsan_requests[0]
    assert request.headers["SOAPAction"] == "urn:vsan/8.0.0.4"
    assert request.headers["Cookie"] == 'vmware_soap_session="vsan-cookie-1"'
    body = request.content.decode("utf-8")
    assert '<VsanQueryVcClusterHealthSummary xmlns="urn:vsan">' in body
    assert '<_this type="VsanVcClusterHealthSystem">vsan-cluster-health-system</_this>' in body
    assert f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>' in body
    assert "<fetchFromCache>" not in body
    # A group with one test comes back as a list, as on VI-JSON.
    assert result == _EXPECTED_FRESH_ANSWER


@pytest.mark.asyncio
async def test_vcenter_8_from_cache_sends_fetch_from_cache_in_soap() -> None:
    """#4922: on vCenter 8.0 ``from_cache: true`` is ``<fetchFromCache>true</fetchFromCache>``."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80([httpx.Response(200, text=_SOAP_SUMMARY_XML)])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            result = await _vsan_health(connector, _StubTarget(), from_cache=True)
    finally:
        await _close(connector)

    assert len(router.vsan_requests) == 1
    body = router.vsan_requests[0].content.decode("utf-8")
    assert (
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
        "<fetchFromCache>true</fetchFromCache></VsanQueryVcClusterHealthSummary>"
    ) in body
    assert result == {**_EXPECTED_FRESH_ANSWER, "from_cache": True}


@pytest.mark.asyncio
async def test_vcenter_8_logs_in_once_for_two_reads() -> None:
    """The vSAN SOAP session is kept per target; a second read needs no new login."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80([httpx.Response(200, text=_SOAP_SUMMARY_XML)])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            versions = _mock_vcenter_80(mock, router)
            target = _StubTarget()
            await _vsan_health(connector, target)
            await _vsan_health(connector, target)
            assert router.sdk_methods == ["RetrieveServiceContent", "Login"]
            assert versions.call_count == 1
            assert len(router.vsan_requests) == 2
            # invalidate_session drops the vSAN session too: the next read logs in again.
            await connector.invalidate_session(target)
            await _vsan_health(connector, target)
    finally:
        await _close(connector)

    assert router.sdk_methods == ["RetrieveServiceContent", "Login"] * 2
    assert router.vsan_requests[2].headers["Cookie"] == 'vmware_soap_session="vsan-cookie-2"'


@pytest.mark.asyncio
async def test_vcenter_8_without_vsan_version_file_sends_empty_soap_action() -> None:
    """When ``vsanServiceVersions.xml`` cannot be read, the service picks its default version."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80([httpx.Response(200, text=_SOAP_SUMMARY_XML)])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router, vsan_version=None)
            result = await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert router.vsan_requests[0].headers["SOAPAction"] == ""
    assert result["overall_health"] == "red"


@pytest.mark.asyncio
async def test_vcenter_8_expired_session_logs_in_again_once() -> None:
    """``NotAuthenticated`` on ``/vsanHealth``: one new login, one retry, then the answer."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80(
        [
            httpx.Response(500, text=_soap_fault("NotAuthenticated", "The session is not valid.")),
            httpx.Response(200, text=_SOAP_SUMMARY_XML),
        ]
    )
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            result = await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert router.sdk_methods == ["RetrieveServiceContent", "Login"] * 2
    assert [r.headers["Cookie"] for r in router.vsan_requests] == [
        'vmware_soap_session="vsan-cookie-1"',
        'vmware_soap_session="vsan-cookie-2"',
    ]
    assert result["groups"] == _EXPECTED_GROUPS


@pytest.mark.asyncio
async def test_vcenter_8_rejected_after_fresh_login_does_not_ask_to_restage() -> None:
    """Still ``NotAuthenticated`` after a new login: the error says the credential works."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80(
        [httpx.Response(500, text=_soap_fault("NotAuthenticated", "The session is not valid."))]
    )
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            with pytest.raises(RuntimeError, match="do NOT restage") as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert len(router.vsan_requests) == 2
    result = result_connector_error(_OP_ID, raised.value, 1.0)
    assert result.extras["upstream_fault"]["type"] == "NotAuthenticated"


@pytest.mark.asyncio
async def test_vcenter_8_vsan_fault_is_an_error_with_the_vendor_text() -> None:
    """A SOAP vSAN fault raises; the error answer carries the fault type and text."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    router = _Vcenter80([httpx.Response(500, text=_soap_fault("VsanFault", _VSAN_FAULT_TEXT))])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            with pytest.raises(RuntimeError) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert "VsanFault" in str(raised.value)
    result = result_connector_error(_OP_ID, raised.value, 1.0)
    assert result.status == "error"
    assert result.extras["upstream_fault"] == {"type": "VsanFault", "faultstring": _VSAN_FAULT_TEXT}


@pytest.mark.asyncio
async def test_vcenter_8_missing_cluster_is_not_found() -> None:
    """A SOAP ``ManagedObjectNotFound`` for the cluster becomes MEHO's not_found."""
    connector = VmwareRestConnector(session_loader=_stub_loader)
    fault = _soap_fault(
        "ManagedObjectNotFound",
        _not_found_text("ClusterComputeResource", _CLUSTER),
        detail=f'<obj type="ClusterComputeResource">{_CLUSTER}</obj>',
    )
    router = _Vcenter80([httpx.Response(500, text=fault)])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            with pytest.raises(ConnectorResourceNotFoundError) as raised:
                await _vsan_health(connector, _StubTarget())
    finally:
        await _close(connector)

    assert raised.value.resource_ids == [_CLUSTER]


# ---------------------------------------------------------------------------
# The vSAN SOAP codec
# ---------------------------------------------------------------------------


def test_build_vsan_envelope_for_the_health_summary() -> None:
    envelope = build_vsan_envelope(
        "VsanVcClusterHealthSystem",
        "vsan-cluster-health-system",
        "VsanQueryVcClusterHealthSummary",
        {"cluster": vim_moref("ClusterComputeResource", "domain-c7")},
    )
    assert (
        '<soapenv:Body><VsanQueryVcClusterHealthSummary xmlns="urn:vsan">'
        '<_this type="VsanVcClusterHealthSystem">vsan-cluster-health-system</_this>'
        '<cluster type="ClusterComputeResource">domain-c7</cluster>'
        "</VsanQueryVcClusterHealthSummary></soapenv:Body>"
    ) in envelope


def test_build_vsan_envelope_sends_true_false_values() -> None:
    """#4922: a true/false argument is written as ``xsd:boolean`` text, after the cluster."""
    envelope = build_vsan_envelope(
        "VsanVcClusterHealthSystem",
        "vsan-cluster-health-system",
        "VsanQueryVcClusterHealthSummary",
        {"cluster": vim_moref("ClusterComputeResource", "domain-c7"), "fetchFromCache": True},
    )
    assert (
        '<cluster type="ClusterComputeResource">domain-c7</cluster>'
        "<fetchFromCache>true</fetchFromCache></VsanQueryVcClusterHealthSummary>"
    ) in envelope
    assert "<includeObjUuids>false</includeObjUuids>" in build_vsan_envelope(
        "VsanVcClusterHealthSystem",
        "vsan-cluster-health-system",
        "VsanQueryVcClusterHealthSummary",
        {"includeObjUuids": False},
    )


@pytest.mark.parametrize(
    ("name", "value", "refused_field"),
    [
        # Numbers, texts, lists and data objects are written since the vSAN
        # changes; a decimal number or another type is still refused, also
        # inside a data object (the error names the inner field).
        ("ratio", 1.5, "ratio"),
        ("spec", {"_typeName": "VsanClusterHealthQuerySpec", "when": object()}, "when"),
    ],
)
def test_build_vsan_envelope_refuses_argument_types_it_cannot_send(
    name: str, value: Any, refused_field: str
) -> None:
    with pytest.raises(TypeError, match=refused_field):
        build_vsan_envelope(
            "VsanVcClusterHealthSystem",
            "vsan-cluster-health-system",
            "VsanQueryVcClusterHealthSummary",
            {name: value},
        )


def test_vsan_soap_action_names_the_vsan_version() -> None:
    assert vsan_soap_action("8.0.0.4") == "urn:vsan/8.0.0.4"
    assert vsan_soap_action(None) == ""


def test_parse_fault_object_reads_the_obj_of_the_fault() -> None:
    fault = _soap_fault(
        "ManagedObjectNotFound",
        "gone",
        detail='<obj type="ClusterComputeResource">domain-c9</obj>',
    )
    assert parse_fault_object(fault) == {"type": "ClusterComputeResource", "value": "domain-c9"}
    assert parse_fault_object(_soap_fault("VsanFault", "no obj")) is None
    assert parse_fault_object("not xml") is None
