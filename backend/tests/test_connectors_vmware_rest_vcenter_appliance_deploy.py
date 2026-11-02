# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests of ``vmware.composite.vcenter.appliance.deploy`` (step 1 of a vCenter install).

The real import engine runs against a recording vCenter double and an
in-memory appliance file. No live vCenter is used; all names and ids are made
up. The tests check what MEHO sends (the OVF settings, the size, the disk
mode), what it returns (each file with its SHA-256), that it cleans up after
an error, that the download session gets keep-alives during a long copy, and
that the root password from Vault is never in an answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from dataclasses import replace
from typing import Any

import httpx
import pytest

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest import library_download, ovf_transfer
from meho_backplane.connectors.vmware_rest import vcenter_deployment as deployment_mod
from meho_backplane.connectors.vmware_rest.composites import _vcenter_deploy, _write
from meho_backplane.connectors.vmware_rest.composites._vcenter_deploy import (
    APPLIANCE_DEPLOY_OP_ID,
    PROP_ADDRESS,
    PROP_DNS_SERVERS,
    PROP_GATEWAY,
    PROP_HOST_NAME,
    PROP_IP_FAMILY,
    PROP_MODE,
    PROP_PREFIX,
    PROP_ROOT_PASSWORD,
    vcenter_appliance_deploy_composite,
    vcenter_appliance_deploy_preview,
)
from meho_backplane.operations._preview import PreviewContext
from meho_backplane.operations.composite import (
    CompositeResumeState,
    composite_resume_state_var,
)

#: A made-up root password with characters that JSON escapes.
_ROOT_PASSWORD = 'Root-canary"7f3a-must-not-leak'
_ITEM_ID = "00000000-0000-0000-0000-0000000004a1"
_DISK = b"appliance-disk-bytes" * 8
#: The upload URL of the lease; its path and query carry one-time tickets.
_TICKET = "52d1-ticket-canary-must-not-leak"
_UPLOAD_URL = f"https://esx.test.invalid/nfc/{_TICKET}/disk-0.vmdk?ticket={_TICKET}"

_PARAMS: dict[str, Any] = {
    "library_item": _ITEM_ID,
    "resource_pool": "resgroup-8",
    "datastore": "datastore-15",
    "folder": "group-v3",
    "port_group": "dvportgroup-21",
    "name": "new-vcenter-vm",
    "size": "small",
    "storage_size": "large",
    "host_name": "new-vcenter.example.test",
    "network": {
        "mode": "static",
        "address": "198.51.100.20",
        "prefix": 24,
        "gateway": "198.51.100.1",
        "dns_servers": ["198.51.100.2", "198.51.100.3"],
    },
    "root_password_secret_ref": "appliance/new-vcenter/root",
}


class _Target:
    name = "vc-existing"
    host = "vc-existing.test.invalid"
    port = 443
    secret_ref = "targets/vc-existing"


class _Operator:
    raw_jwt = "jwt"


class _Resp:
    """The upload answer; a failure raises the real httpx error, which names the URL."""

    def __init__(self, status: int, url: str) -> None:
        self.status_code = status
        self._response = httpx.Response(status, request=httpx.Request("PUT", url))

    def raise_for_status(self) -> None:
        self._response.raise_for_status()


class _PutClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.puts: list[dict[str, Any]] = []
        self._fail = fail

    async def put(self, url: str, *, content: Any, **_kw: Any) -> _Resp:
        received = b""
        async for chunk in content:
            received += chunk
        self.puts.append({"url": url, "bytes": received})
        return _Resp(403 if self._fail else 200, url)


class _Disk:
    def __init__(self, data: bytes, *, chunk_sleep: float = 0.0) -> None:
        self._data = data
        self.size = len(data)
        self._sleep = chunk_sleep

    async def __aenter__(self) -> _Disk:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def aiter_bytes(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        half = len(self._data) // 2
        for part in (self._data[:half], self._data[half:]):
            if self._sleep:
                await asyncio.sleep(self._sleep)
            yield part


class _UnpreparedDisk:
    """A disk whose download-session preparation fails when it is opened."""

    async def __aenter__(self) -> _UnpreparedDisk:
        raise library_download.LibraryDownloadError(
            "download-session file 'appliance-disk1.vmdk' failed to prepare (status=ERROR)"
        )

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _Source:
    """The content-library download session, in memory."""

    instances: list[_Source] = []  # noqa: RUF012 -- reset by each test

    def __init__(
        self, *, chunk_sleep: float = 0.0, prepare_fails: bool = False, **_kw: Any
    ) -> None:
        self.keep_alives = 0
        self.closed = False
        self._chunk_sleep = chunk_sleep
        self._prepare_fails = prepare_fails
        _Source.instances.append(self)

    async def read_descriptor(self) -> str:
        return "<Envelope/>"

    def open_disk(self, name: str) -> Any:
        assert name == "appliance-disk1.vmdk"
        if self._prepare_fails:
            return _UnpreparedDisk()
        return _Disk(_DISK, chunk_sleep=self._chunk_sleep)

    async def keep_alive(self) -> None:
        self.keep_alives += 1

    async def aclose(self) -> None:
        self.closed = True


def _ready_poll() -> dict[str, Any]:
    info = {
        "_typeName": "HttpNfcLeaseInfo",
        "lease": {"type": "HttpNfcLease", "value": "lease-1"},
        "entity": {"type": "VirtualMachine", "value": "vm-77"},
        "deviceUrl": [
            {
                "_typeName": "HttpNfcLeaseDeviceUrl",
                "key": "/vm-77/disk-0",
                "importKey": "disk1",
                "url": _UPLOAD_URL,
                "disk": True,
            }
        ],
    }
    return {
        "objects": [{"propSet": [{"name": "state", "val": "ready"}, {"name": "info", "val": info}]}]
    }


class _VCenter:
    """Recording vCenter double: the VI-JSON calls and the REST calls of the deploy."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(
        self,
        *,
        warnings: list[Any] | None = None,
        import_spec_fault: httpx.HTTPStatusError | None = None,
        upload_fail: bool = False,
    ) -> None:
        self.vmomi: list[tuple[str, Any]] = []
        self.rest: list[tuple[str, str, Any]] = []
        self._warnings = warnings or []
        self._fault = import_spec_fault
        self.upload_client = _PutClient(fail=upload_fail)

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Any, json: Any = None
    ) -> Any:
        self.vmomi.append((path, json))
        if path.endswith("/RetrieveServiceContent"):
            return {
                "ovfManager": {"type": "OvfManager", "value": "OvfManager"},
                "rootFolder": {"type": "Folder", "value": "group-d1"},
            }
        if path.endswith("/CreateImportSpec"):
            if self._fault is not None:
                raise self._fault
            return {
                "importSpec": {"_typeName": "VirtualMachineImportSpec"},
                "fileItem": [
                    {"deviceId": "disk1", "path": "appliance-disk1.vmdk", "size": len(_DISK)}
                ],
                "error": [],
                "warning": self._warnings,
            }
        if path.endswith("/ImportVApp"):
            return {"type": "HttpNfcLease", "value": "lease-1"}
        if path.endswith("/RetrievePropertiesEx"):
            return _ready_poll()
        return {}

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return f"/api{path}"

    async def _post_json(
        self, target: Any, path: str, *, operator: Any, verb: str = "POST", **kw: Any
    ) -> Any:
        self.rest.append((verb, path, kw.get("json")))
        return {}

    def spec_body(self) -> dict[str, Any]:
        return next(body for path, body in self.vmomi if path.endswith("/CreateImportSpec"))

    def import_body(self) -> dict[str, Any]:
        return next(body for path, body in self.vmomi if path.endswith("/ImportVApp"))

    def paths(self) -> list[str]:
        return [path for path, _ in self.vmomi]


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Vault, the download source, the upload client and the write gate, all faked."""
    _Source.instances = []

    async def _vault(target: Any, operator: Any, *, param: str, mount: str) -> dict[str, Any]:
        assert target.secret_ref == _PARAMS["root_password_secret_ref"]
        assert param == "root_password_secret_ref"
        return {"password": _ROOT_PASSWORD}

    monkeypatch.setattr(deployment_mod, "load_param_secret_data", _vault)
    monkeypatch.setattr(library_download, "LibraryDownloadSource", _Source)

    @contextlib.asynccontextmanager
    async def _client(target: Any, thumbprint: Any, certificate: Any = None) -> Any:
        yield _CONNECTOR_BOX["vc"].upload_client

    monkeypatch.setattr(ovf_transfer, "upload_client", _client)
    gate_calls: list[dict[str, Any]] = []

    async def _gate(**kwargs: Any) -> OperationResult | None:
        gate_calls.append(kwargs)
        return None

    monkeypatch.setattr(_write, "enforce_subop_policy", _gate)
    return gate_calls


_CONNECTOR_BOX: dict[str, _VCenter] = {}


async def _deploy(vc: _VCenter, params: dict[str, Any] | None = None) -> Any:
    _CONNECTOR_BOX["vc"] = vc
    return await vcenter_appliance_deploy_composite(
        operator=_Operator(),  # type: ignore[arg-type]
        target=_Target(),
        params=dict(params or _PARAMS),
        connector=vc,  # type: ignore[arg-type]
    )


def _assert_no_password(value: Any) -> None:
    blob = json.dumps(value, default=str)
    assert _ROOT_PASSWORD not in blob
    assert json.dumps(_ROOT_PASSWORD)[1:-1] not in blob


async def test_deploy_imports_with_the_appliance_settings_and_powers_on(
    _fakes: list[dict[str, Any]],
) -> None:
    vc = _VCenter()
    out = await _deploy(vc)

    assert out["status"] == "deployed"
    assert out["vm_id"] == "vm-77"
    assert out["powered_on"] is True
    assert out["deployment_option"] == "small-lstorage"
    cisp = vc.spec_body()["cisp"]
    assert cisp["deploymentOption"] == "small-lstorage"
    assert cisp["diskProvisioning"] == "thin"
    assert cisp["entityName"] == "new-vcenter-vm"
    assert cisp["networkMapping"][0]["name"] == "Network 1"
    assert cisp["networkMapping"][0]["network"]["value"] == "dvportgroup-21"
    props = {entry["key"]: entry["value"] for entry in cisp["propertyMapping"]}
    assert props == {
        PROP_IP_FAMILY: "ipv4",
        PROP_MODE: "static",
        PROP_ADDRESS: "198.51.100.20",
        PROP_PREFIX: "24",
        PROP_GATEWAY: "198.51.100.1",
        PROP_DNS_SERVERS: "198.51.100.2,198.51.100.3",
        PROP_HOST_NAME: "new-vcenter.example.test",
        PROP_ROOT_PASSWORD: _ROOT_PASSWORD,
    }
    # Only the user-configurable settings: no hidden guestinfo.cis.deployment.* ones.
    assert not [key for key in props if "deployment" in key or "vmdir" in key]
    assert out["settings_sent"] == sorted(props)

    # The final answer lists each file with its size and SHA-256, after the clean-up.
    assert out["transfer"] == [
        {
            "path": "appliance-disk1.vmdk",
            "device_id": "disk1",
            "size_bytes": len(_DISK),
            "sha256": hashlib.sha256(_DISK).hexdigest(),
        }
    ]
    assert vc.upload_client.puts[0]["bytes"] == _DISK
    assert vc.import_body()["folder"]["value"] == "group-v3"
    assert vc.paths()[-1].endswith("/HttpNfcLeaseComplete")
    assert _Source.instances[0].closed is True
    assert vc.rest == [("POST", "/api/vcenter/vm/vm-77/power?action=start", None)]
    assert [call["op_id"] for call in _fakes] == [
        "POST:/ResourcePool/{moId}/ImportVApp",
        "POST:/vcenter/vm/{vm}/power?action=start",
    ]
    _assert_no_password(out)
    _assert_no_password([call["params"] for call in _fakes])


async def test_dhcp_without_power_on_sends_no_address() -> None:
    params = {**_PARAMS, "network": {"mode": "dhcp"}, "power_on": False, "disk_mode": "thick"}
    params.pop("storage_size")
    vc = _VCenter()
    out = await _deploy(vc, params)

    assert out["status"] == "deployed"
    assert out["powered_on"] is False
    assert out["deployment_option"] == "small"
    cisp = vc.spec_body()["cisp"]
    assert cisp["diskProvisioning"] == "thick"
    keys = {entry["key"] for entry in cisp["propertyMapping"]}
    assert keys == {PROP_IP_FAMILY, PROP_MODE, PROP_HOST_NAME, PROP_ROOT_PASSWORD}
    assert vc.rest == []


async def test_a_failed_copy_aborts_the_lease_and_still_cleans_up() -> None:
    vc = _VCenter(upload_fail=True)
    out = await _deploy(vc)

    assert out["status"] == "deploy_error"
    assert out["vm_id"] is None
    assert out["powered_on"] is False
    assert out["transfer"] == []
    assert any(issue["severity"] == "error" for issue in out["issues"])
    assert vc.paths()[-1].endswith("/HttpNfcLeaseAbort")
    assert not any(path.endswith("/HttpNfcLeaseComplete") for path in vc.paths())
    assert _Source.instances[0].closed is True
    assert vc.rest == []


async def test_an_upload_error_does_not_show_the_upload_url_or_its_ticket() -> None:
    """The real httpx error names the upload URL; the result must not."""
    vc = _VCenter(upload_fail=True)
    out = await _deploy(vc)

    assert out["status"] == "deploy_error"
    errors = [issue["message"] for issue in out["issues"] if issue["severity"] == "error"]
    assert errors and "403" in errors[0]
    assert "<url hidden>" in errors[0]
    blob = json.dumps(out)
    assert _TICKET not in blob
    assert _UPLOAD_URL not in blob
    assert "https://" not in blob
    assert vc.upload_client.puts[0]["url"] == _UPLOAD_URL


async def test_a_disk_that_fails_to_prepare_aborts_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A download-session error after ImportVApp ends the lease, so no half VM stays."""

    def _failing_source(**kw: Any) -> _Source:
        return _Source(prepare_fails=True, **kw)

    monkeypatch.setattr(library_download, "LibraryDownloadSource", _failing_source)
    vc = _VCenter()
    out = await _deploy(vc)

    assert out["status"] == "deploy_error"
    assert out["vm_id"] is None
    assert out["transfer"] == []
    assert "failed to prepare" in out["issues"][0]["message"]
    assert vc.upload_client.puts == []
    assert vc.paths()[-1].endswith("/HttpNfcLeaseAbort")
    assert not any(path.endswith("/HttpNfcLeaseComplete") for path in vc.paths())
    assert _Source.instances[0].closed is True
    assert vc.rest == []


async def test_a_call_without_a_folder_is_refused_before_anything_is_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(**_kw: Any) -> Any:
        raise AssertionError("a refused call must not read Vault")

    monkeypatch.setattr(deployment_mod, "load_param_secret_data", _must_not_run)
    params = {key: value for key, value in _PARAMS.items() if key != "folder"}
    vc = _VCenter()
    out = await _deploy(vc, params)

    assert out["status"] == "folder_required"
    assert out["vm_id"] is None
    assert out["issues"][0]["severity"] == "error"
    assert "folder is required" in out["issues"][0]["message"]
    assert vc.vmomi == []
    assert vc.rest == []
    assert _Source.instances == []


async def test_a_vendor_fault_that_echoes_the_password_is_hidden() -> None:
    request = httpx.Request("POST", "https://vc-existing.test.invalid/sdk/vim25/9.0.0.0/x")
    fault = {
        "_typeName": "InvalidArgument",
        "faultstring": f"bad value {_ROOT_PASSWORD} for property",
    }
    exc = httpx.HTTPStatusError(
        "fault", request=request, response=httpx.Response(500, json=fault, request=request)
    )
    vc = _VCenter(import_spec_fault=exc)
    out = await _deploy(vc)

    assert out["status"] == "deploy_error"
    assert "hid this text" in out["issues"][0]["message"]
    assert _Source.instances[0].closed is True
    assert not any(path.endswith("/ImportVApp") for path in vc.paths())
    _assert_no_password(out)


async def test_settings_vcenter_did_not_use_are_named() -> None:
    warning = {
        "_typeName": "OvfUnsupportedProperty",
        "faultstring": (
            f"Found non user configurable properties that are not used: {PROP_DNS_SERVERS}"
        ),
    }
    vc = _VCenter(warnings=[warning])
    out = await _deploy(vc)

    assert out["status"] == "deployed"
    assert out["settings_not_used"] == [PROP_DNS_SERVERS]
    assert any(issue["severity"] == "warning" for issue in out["issues"])


async def test_a_long_copy_sends_keep_alives_to_the_download_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_vcenter_deploy, "_HEARTBEAT_INTERVAL", 0.01)

    def _slow_source(**kw: Any) -> _Source:
        return _Source(chunk_sleep=0.05, **kw)

    monkeypatch.setattr(library_download, "LibraryDownloadSource", _slow_source)
    vc = _VCenter()
    out = await _deploy(vc)

    assert out["status"] == "deployed"
    assert _Source.instances[0].keep_alives >= 1
    assert any(path.endswith("/HttpNfcLeaseProgress") for path in vc.paths())


async def test_a_parked_import_gate_comes_back_and_nothing_is_imported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parked = OperationResult(
        status="awaiting_approval",
        op_id="POST:/ResourcePool/{moId}/ImportVApp",
        duration_ms=0.0,
        extras={"approval_request_id": "req-1"},
    )

    async def _park(**_kw: Any) -> OperationResult:
        return parked

    monkeypatch.setattr(_write, "enforce_subop_policy", _park)
    vc = _VCenter()
    out = await _deploy(vc)

    assert out is parked
    assert not any(path.endswith("/ImportVApp") for path in vc.paths())
    assert _Source.instances[0].closed is True


async def test_the_resume_of_an_approved_power_on_only_powers_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _must_not_run(**_kw: Any) -> Any:
        raise AssertionError("the resume must not read Vault or import again")

    monkeypatch.setattr(deployment_mod, "load_param_secret_data", _must_not_run)
    imported = {"status": "deployed", "vm_id": "vm-77", "issues": [], "powered_on": False}
    token = composite_resume_state_var.set(
        CompositeResumeState(
            op_id=APPLIANCE_DEPLOY_OP_ID, state={"imported": imported}, after_main_effect=True
        )
    )
    vc = _VCenter()
    try:
        out = await _deploy(vc)
    finally:
        composite_resume_state_var.reset(token)
    assert out == {**imported, "powered_on": True}
    assert vc.vmomi == []
    assert vc.rest == [("POST", "/api/vcenter/vm/vm-77/power?action=start", None)]


async def test_an_unknown_library_item_name_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _resolve(**_kw: Any) -> Any:
        return None, {"status": "item_not_found", "issues": [], "candidates": None}

    monkeypatch.setattr(_vcenter_deploy, "_resolve_deploy_library_item", _resolve)
    params = {k: v for k, v in _PARAMS.items() if k != "library_item"}
    vc = _VCenter()
    out = await _deploy(vc, {**params, "library_item_name": "<vcenter-appliance-item>"})

    assert out["status"] == "item_not_found"
    assert vc.vmomi == []
    assert _Source.instances == []


async def test_the_preview_shows_the_plan_and_never_the_password() -> None:
    ctx = PreviewContext(
        descriptor=None,  # type: ignore[arg-type]
        connector_instance=None,
        operator=_Operator(),  # type: ignore[arg-type]
        target=_Target(),
        params=dict(_PARAMS),
    )
    preview = await vcenter_appliance_deploy_preview(ctx)

    assert preview is not None
    assert preview["deployment_option"] == "small-lstorage"
    assert preview["folder"] == "group-v3"
    assert preview["root_password_secret_ref"] == _PARAMS["root_password_secret_ref"]
    assert preview["host_name"] == "new-vcenter.example.test"
    _assert_no_password(preview)
    assert await vcenter_appliance_deploy_preview(replace(ctx, params={"name": "x"})) is None
