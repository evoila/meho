# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The vSAN changes on the real connector, over a mocked vCenter 8 and 9 wire.

* vCenter 9.1: the vSAN methods are VI-JSON ``/sdk/vim25/9.1.0.0/vsan/<Type>/
  <moId>/<Method>``, and ``QueryDisksForVsan`` is at ``/vsan/HostVsanSystem/...``.
* vCenter 8.0.3: the vSAN methods go as SOAP to ``/vsanHealth``. The data
  objects of the spec are written as nested XML in the vendor field order,
  and ``QueryDisksForVsan`` is the VI-JSON path ``/HostVsanSystem/...`` of the
  8.0 files.

The answers are hand-made in the shape of the vendor types, with made-up ids.
The sub-op gate is replaced by a recorder; the approval flow is tested in
``test_connectors_vmware_rest_vsan_changes_approval.py``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import (
    _vsan_cluster,
    _vsan_common,
    _vsan_disk_claim,
)
from meho_backplane.settings import get_settings
from tests._vmware_teardown_fake import GateRecorder, operator
from tests._vmware_vsan_fake import scsi_disk
from tests.test_connectors_vmware_rest_vsan_transport import (
    _BASE,
    _close,
    _mock_vcenter_80,
    _mock_vcenter_session,
    _soap_envelope,
    _soap_fault,
    _stub_loader,
    _StubTarget,
    _Vcenter80,
)

_CLUSTER = "domain-c7"
_HOST = "host-12"
_DISK_A = "naa.0000000000000a"
_DISK_B = "naa.0000000000000b"


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv("VAULT_ADDR", "https://vault.test")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _gate(monkeypatch: pytest.MonkeyPatch) -> GateRecorder:
    recorder = GateRecorder()
    monkeypatch.setattr(_vsan_common, "enforce_subop_policy", recorder)
    monkeypatch.setattr(_vsan_disk_claim, "_TASK_TIMEOUT_SECONDS", 5.0)
    return recorder


def _moref(mo_type: str, moid: str) -> dict[str, str]:
    return {"_typeName": "ManagedObjectReference", "type": mo_type, "value": moid}


class _VimJson:
    """Answers the VI-JSON calls: object reads, task reads and QueryDisksForVsan."""

    def __init__(self) -> None:
        self.disk_state = "eligible"
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        body = json.loads(request.content or b"{}")
        if path.endswith("/RetrievePropertiesEx"):
            spec = body["specSet"][0]
            mo_type = spec["propSet"][0]["type"]
            moid = spec["objectSet"][0]["obj"]["value"]
            return httpx.Response(200, json={"objects": [self._object(mo_type, moid)]})
        if path.endswith("/QueryDisksForVsan"):
            return httpx.Response(
                200,
                json=[
                    {
                        "_typeName": "VsanHostDiskResult",
                        "disk": scsi_disk(name),
                        "state": self.disk_state,
                    }
                    for name in body["canonicalName"]
                ],
            )
        return httpx.Response(500, json={"_typeName": "RuntimeFault", "faultstring": path})

    def _object(self, mo_type: str, moid: str) -> dict[str, Any]:
        if mo_type == "ClusterComputeResource":
            props: dict[str, Any] = {"name": "cluster-a", "recentTask": []}
        elif mo_type == "HostSystem":
            props = {
                "name": "esxi-a",
                "configManager.vsanSystem": _moref("HostVsanSystem", "vsanSystem-12"),
                "configManager.storageSystem": _moref("HostStorageSystem", "storageSystem-12"),
            }
        else:  # Task: the claim ends well and the disks are in use after it.
            self.disk_state = "inUse"
            props = {"info": {"_typeName": "TaskInfo", "key": moid, "state": "success"}}
        return {
            "obj": {"type": mo_type, "value": moid},
            "propSet": [{"name": key, "val": value} for key, value in props.items()],
        }


async def _configure(connector: VmwareRestConnector) -> Any:
    return await _vsan_cluster.vsan_cluster_configure_composite(
        operator=operator(),
        target=_StubTarget(),
        params={"cluster": _CLUSTER, "vsan_enabled": True, "esa": True},
        connector=connector,
    )


# ---------------------------------------------------------------------------
# vsan.cluster.configure
# ---------------------------------------------------------------------------


async def test_vcenter_9_configure_posts_the_spec_on_the_vsan_vi_json_path() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    vim = _VimJson()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            reconfig = mock.post(
                "/sdk/vim25/9.1.0.0/vsan/VsanVcClusterConfigSystem/"
                "vsan-cluster-config-system/VsanClusterReconfig"
            ).respond(200, json=_moref("Task", "task-9"))
            mock.post(path__startswith="/sdk/vim25/9.1.0.0/PropertyCollector/").mock(
                side_effect=vim
            )
            soap = mock.post("/vsanHealth").respond(500)
            out = await _configure(connector)
    finally:
        await _close(connector)

    assert out["status"] == "accepted"
    assert out["task"] == "task-9"
    assert not soap.called
    assert json.loads(reconfig.calls[0].request.content) == {
        "cluster": _moref("ClusterComputeResource", _CLUSTER),
        "vsanReconfigSpec": {
            "_typeName": "VimVsanReconfigSpec",
            "vsanClusterConfig": {
                "_typeName": "VsanClusterConfigInfo",
                "enabled": True,
                "vsanEsaEnabled": True,
            },
            "modify": True,
        },
    }


async def test_vcenter_8_configure_sends_the_spec_as_soap_in_vendor_order() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    answer = _soap_envelope(
        '<VsanClusterReconfigResponse xmlns="urn:vsan">'
        '<returnval type="Task">task-9</returnval></VsanClusterReconfigResponse>'
    )
    router = _Vcenter80([httpx.Response(200, text=answer)])
    vim = _VimJson()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            mock.post(path__startswith="/sdk/vim25/8.0.3.0/PropertyCollector/").mock(
                side_effect=vim
            )
            out = await _configure(connector)
    finally:
        await _close(connector)

    assert out["status"] == "accepted"
    assert out["task"] == "task-9"
    body = router.vsan_requests[0].content.decode("utf-8")
    assert (
        '<VsanClusterReconfig xmlns="urn:vsan">'
        '<_this type="VsanVcClusterConfigSystem">vsan-cluster-config-system</_this>'
        f'<cluster type="ClusterComputeResource">{_CLUSTER}</cluster>'
        "<vsanReconfigSpec><vsanClusterConfig><enabled>true</enabled>"
        "<vsanEsaEnabled>true</vsanEsaEnabled></vsanClusterConfig>"
        "<modify>true</modify></vsanReconfigSpec></VsanClusterReconfig>"
    ) in body
    assert "_typeName" not in body


async def test_vcenter_8_configure_vsan_fault_reaches_the_caller() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    text = "vSAN cannot be enabled on this cluster."
    router = _Vcenter80([httpx.Response(500, text=_soap_fault("VsanFault", text))])
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            mock.post(path__startswith="/sdk/vim25/8.0.3.0/PropertyCollector/").mock(
                side_effect=_VimJson()
            )
            with pytest.raises(RuntimeError) as raised:
                await _configure(connector)
    finally:
        await _close(connector)

    assert "VsanClusterReconfig" in str(raised.value)
    assert "VsanFault" in str(raised.value) or text in str(raised.value)


# ---------------------------------------------------------------------------
# vsan.disk.claim
# ---------------------------------------------------------------------------


async def _claim_osa(connector: VmwareRestConnector) -> Any:
    return await _vsan_disk_claim.vsan_disk_claim_composite(
        operator=operator(),
        target=_StubTarget(),
        params={
            "host": _HOST,
            "architecture": "osa",
            "cache_disks": [_DISK_A],
            "capacity_disks": [_DISK_B],
        },
        connector=connector,
    )


async def test_vcenter_9_claim_reads_disks_on_the_vsan_path() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    vim = _VimJson()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_session(mock, "9.1.0.0")
            init = mock.post(
                "/sdk/vim25/9.1.0.0/vsan/VimClusterVsanVcDiskManagementSystem/"
                "vsan-disk-management-system/InitializeDiskMappings"
            ).respond(200, json=_moref("Task", "task-5"))
            mock.post(path__startswith="/sdk/vim25/9.1.0.0/PropertyCollector/").mock(
                side_effect=vim
            )
            mock.post(path__startswith="/sdk/vim25/9.1.0.0/vsan/HostVsanSystem/").mock(
                side_effect=vim
            )
            out = await _claim_osa(connector)
    finally:
        await _close(connector)

    assert out["status"] == "claimed"
    assert "/sdk/vim25/9.1.0.0/vsan/HostVsanSystem/vsanSystem-12/QueryDisksForVsan" in vim.paths
    spec = json.loads(init.calls[0].request.content)["spec"]
    assert spec["cacheDisks"][0]["canonicalName"] == _DISK_A
    assert spec["capacityDisks"][0]["canonicalName"] == _DISK_B
    assert spec["creationType"] == "allFlash"


async def test_vcenter_8_claim_sends_the_disk_objects_as_soap() -> None:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    answer = _soap_envelope(
        '<InitializeDiskMappingsResponse xmlns="urn:vsan">'
        '<returnval type="Task">task-5</returnval></InitializeDiskMappingsResponse>'
    )
    router = _Vcenter80([httpx.Response(200, text=answer)])
    vim = _VimJson()
    try:
        async with respx.mock(base_url=_BASE, assert_all_called=False) as mock:
            _mock_vcenter_80(mock, router)
            mock.post(path__startswith="/sdk/vim25/8.0.3.0/PropertyCollector/").mock(
                side_effect=vim
            )
            mock.post(path__startswith="/sdk/vim25/8.0.3.0/HostVsanSystem/").mock(side_effect=vim)
            out = await _claim_osa(connector)
    finally:
        await _close(connector)

    assert out["status"] == "claimed"
    assert "/sdk/vim25/8.0.3.0/HostVsanSystem/vsanSystem-12/QueryDisksForVsan" in vim.paths
    body = router.vsan_requests[0].content.decode("utf-8")
    assert '<_this type="VimClusterVsanVcDiskManagementSystem">' in body
    assert f'<spec><host type="HostSystem">{_HOST}</host><cacheDisks>' in body
    assert f"<canonicalName>{_DISK_A}</canonicalName>" in body
    assert "<operationalState>ok</operationalState>" in body
    assert "<capacity><blockSize>512</blockSize><block>1000</block></capacity>" in body
    assert "<creationType>allFlash</creationType></spec>" in body
    assert "_typeName" not in body
