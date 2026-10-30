# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Unit tests for the typed HttpNfcLease OVF import engine (#3229).

Proves the acceptance criteria with a recording connector + an in-memory
fake source (no live vCenter): a lease import completes regardless of any
single-request window (AC1), progress is surfaced via the lease heartbeat
(AC2), the deploy-envelope error family is preserved (AC4), and only
version-agnostic vim fields are read (AC5). Composite-handler wiring
(resolution reuse, envelope mapping, source cleanup, power-on) is tested
against ``_write.vm_import_from_library_composite``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest import ovf_import, ovf_transfer
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.library_download import LibraryDownloadError
from meho_backplane.connectors.vmware_rest.ovf_import import ImportPlacement, LeaseImportResult


class _Target:
    def __init__(self, host: str = "vcenter.lab", name: str = "vc-1") -> None:
        self.host = host
        self.name = name


@pytest.fixture(autouse=True)
def fake_upload_client(monkeypatch: pytest.MonkeyPatch):
    """Engine tests record uploads; the real TLS path has its own socket tests."""

    materials = []

    @contextlib.asynccontextmanager
    async def client(target: Any, thumbprint: Any, certificate: Any = None):
        materials.append((thumbprint, certificate))
        yield target.upload_client

    monkeypatch.setattr(ovf_transfer, "upload_client", client)
    return materials


class _Operator:
    raw_jwt = "jwt"


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class _PutClient:
    """Fake upload client: records PUTs and drains the streamed body."""

    def __init__(self, *, fail: bool = False) -> None:
        self.puts: list[dict[str, Any]] = []
        self._fail = fail

    async def put(
        self,
        url: str,
        *,
        content: Any,
        headers: dict[str, str],
        timeout: Any,
        follow_redirects: bool,
    ) -> _Resp:
        assert follow_redirects is False
        drained = 0
        async for chunk in content:
            drained += len(chunk)
        self.puts.append({"url": url, "headers": headers, "drained": drained})
        return _Resp(500 if self._fail else 200)


class _EngineConnector:
    """Recording stand-in for VmwareRestConnector on the engine path."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(
        self,
        *,
        service_content: dict[str, Any],
        import_spec_result: dict[str, Any],
        lease_ref: Any,
        lease_polls: list[dict[str, Any]],
        put_client: _PutClient | None = None,
    ) -> None:
        self._service_content = service_content
        self._import_spec_result = import_spec_result
        self._lease_ref = lease_ref
        self._lease_polls = list(lease_polls)
        self._client = put_client or _PutClient()
        self.vmomi_calls: list[tuple[str, Any]] = []

    async def _post_vmomi_json(
        self, target: Any, path: str, *, operator: Any, json: Any = None
    ) -> Any:
        self.vmomi_calls.append((path, json))
        if path.endswith("/RetrieveServiceContent"):
            return self._service_content
        if path.endswith("/CreateImportSpec"):
            return self._import_spec_result
        if path.endswith("/ImportVApp"):
            return self._lease_ref
        if path.endswith("/RetrievePropertiesEx"):
            poll = self._lease_polls.pop(0) if len(self._lease_polls) > 1 else self._lease_polls[0]
            return poll
        return {}


class _FakeDisk:
    def __init__(self, data: bytes, *, chunk_sleep: float = 0.0, chunks: int = 1) -> None:
        self._data = data
        self.size = len(data)
        self._chunk_sleep = chunk_sleep
        self._chunks = chunks

    async def __aenter__(self) -> _FakeDisk:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    def aiter_bytes(self) -> Any:
        return self._gen()

    async def _gen(self) -> Any:
        step = max(1, len(self._data) // max(self._chunks, 1))
        for i in range(0, len(self._data), step):
            if self._chunk_sleep:
                await asyncio.sleep(self._chunk_sleep)
            yield self._data[i : i + step]


class _FakeSource:
    def __init__(self, descriptor: str, disks: dict[str, _FakeDisk]) -> None:
        self._descriptor = descriptor
        self._disks = disks
        self.keep_alives = 0
        self.closed = False

    async def read_descriptor(self) -> str:
        return self._descriptor

    def open_disk(self, name: str) -> _FakeDisk:
        return self._disks[name]

    async def keep_alive(self) -> None:
        self.keep_alives += 1

    async def aclose(self) -> None:
        self.closed = True


def _service_content() -> dict[str, Any]:
    return {
        "ovfManager": {"type": "OvfManager", "value": "OvfManager"},
        "rootFolder": {"type": "Folder", "value": "group-d1"},
    }


def _import_spec_result(
    *, errors: list[Any] | None = None, warnings: list[Any] | None = None
) -> dict[str, Any]:
    return {
        "importSpec": {
            "_typeName": "VirtualMachineImportSpec",
            "configSpec": {"_typeName": "VirtualMachineConfigSpec", "name": "app"},
        },
        "fileItem": [
            {
                "_typeName": "OvfFileItem",
                "deviceId": "disk1",
                "path": "app-disk1.vmdk",
                "size": 8,
                "cimType": 17,
                "create": True,
            }
        ],
        "error": errors or [],
        "warning": warnings or [],
    }


def _ready_poll(
    *,
    with_ssl_cert: bool = False,
    ssl_thumbprint: str | None = None,
    url: str = "https://*/nfc/lease-1/disk-0.vmdk",
) -> dict[str, Any]:
    device_url: dict[str, Any] = {
        "_typeName": "HttpNfcLeaseDeviceUrl",
        "key": "/vm-9/disk-0",
        "importKey": "disk1",
        "url": url,
        "disk": True,
    }
    if ssl_thumbprint is not None:
        device_url["sslThumbprint"] = ssl_thumbprint
    if with_ssl_cert:  # 9.0 lease certificate passed to the upload checker
        device_url["sslCertificate"] = "-----BEGIN CERTIFICATE-----"
    info = {
        "_typeName": "HttpNfcLeaseInfo",
        "lease": {"type": "HttpNfcLease", "value": "lease-1"},
        "entity": {"type": "VirtualMachine", "value": "vm-9"},
        "deviceUrl": [device_url],
        "totalDiskCapacityInKB": 8,
        "leaseTimeout": 300,
    }
    prop_set = [{"name": "state", "val": "ready"}, {"name": "info", "val": info}]
    return {"objects": [{"propSet": prop_set}]}


def _lease_ref() -> dict[str, Any]:
    return {"type": "HttpNfcLease", "value": "lease-1"}


async def _run(connector: _EngineConnector, source: _FakeSource, **kw: Any) -> Any:
    async def _gate(_params: dict[str, Any]) -> None:
        return None

    target = _Target()
    target.upload_client = connector._client
    return await ovf_import.import_ovf_from_source(
        connector=connector,  # type: ignore[arg-type]
        target=target,
        operator=_Operator(),  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        placement=ImportPlacement(resource_pool="rp-1", datastore="ds-1", entity_name="app"),
        gate=kw.pop("gate", _gate),
        lease_ready_timeout=kw.pop("lease_ready_timeout", None),
        heartbeat_interval=kw.pop("heartbeat_interval", 30.0),
    )


async def test_import_success_streams_disk_and_completes() -> None:
    """Full happy path: descriptor -> import -> lease-ready -> disk PUT -> Complete."""
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert result.vm_id == "vm-9"
    assert result.resource_type == "VirtualMachine"
    assert result.transfer == [
        {
            "path": "app-disk1.vmdk",
            "device_id": "disk1",
            "size_bytes": 8,
            "sha256": hashlib.sha256(b"12345678").hexdigest(),
        }
    ]
    # The disk was PUT to the substituted (concrete-host) device URL and Complete ran.
    assert conn._client.puts[0]["url"] == "https://vcenter.lab/nfc/lease-1/disk-0.vmdk"
    assert conn._client.puts[0]["headers"]["Content-Type"] == "application/x-vnd.vmware-streamVmdk"
    methods = [p.rsplit("/", 1)[-1] for p, _ in conn.vmomi_calls]
    assert methods[-1] == "HttpNfcLeaseComplete"


async def test_import_spec_round_trips_verbatim_into_import_vapp() -> None:
    """The server-issued importSpec is passed to ImportVApp with its _typeName intact."""
    spec_result = _import_spec_result()
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=spec_result,
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    await _run(conn, source)
    import_vapp_body = next(body for path, body in conn.vmomi_calls if path.endswith("/ImportVApp"))
    assert import_vapp_body["spec"] == spec_result["importSpec"]
    assert import_vapp_body["spec"]["_typeName"] == "VirtualMachineImportSpec"


async def test_descriptor_errors_short_circuit_to_import_failed() -> None:
    """A CreateImportSpec error maps to deploy_failed-family import_failed, no lease."""
    errors = [{"_typeName": "OvfNetworkMappingNotSupported", "localizedMessage": "bad network"}]
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(errors=errors),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )
    result = await _run(conn, _FakeSource("<Envelope/>", {}))
    assert isinstance(result, LeaseImportResult)
    assert result.status == "import_failed"
    assert result.issues[0]["message"] == "bad network"
    assert not any(p.endswith("/ImportVApp") for p, _ in conn.vmomi_calls)


async def test_import_vapp_gate_park_returns_operation_result() -> None:
    """A parked ImportVApp gate returns the OperationResult verbatim; no write fires."""
    parked = OperationResult(
        status="pending", op_id="vmware.composite.vm.import_from_library", duration_ms=0.0
    )
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )

    async def _gate(_params: dict[str, Any]) -> OperationResult:
        return parked

    result = await _run(conn, _FakeSource("<Envelope/>", {}), gate=_gate)
    assert result is parked
    assert not any(p.endswith("/ImportVApp") for p, _ in conn.vmomi_calls)


async def test_lease_error_state_maps_to_lease_error() -> None:
    """A lease that reaches the error state surfaces the fault as lease_error."""
    error_poll = {
        "objects": [
            {
                "propSet": [
                    {"name": "state", "val": "error"},
                    {"name": "error", "val": {"localizedMessage": "datastore full"}},
                ]
            }
        ]
    }
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[error_poll],
    )
    result = await _run(conn, _FakeSource("<Envelope/>", {}))
    assert isinstance(result, LeaseImportResult)
    assert result.status == "lease_error"
    assert result.issues[0]["message"] == "datastore full"


async def test_lease_timeout_aborts_and_maps_to_lease_timeout() -> None:
    """A lease that never reaches ready times out, aborts, and maps to lease_timeout."""
    initializing = {"objects": [{"propSet": [{"name": "state", "val": "initializing"}]}]}
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[initializing],
    )
    result = await _run(conn, _FakeSource("<Envelope/>", {}), lease_ready_timeout=0.0)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "lease_timeout"
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)


async def test_upload_fault_aborts_and_maps_to_import_error() -> None:
    """A disk-upload transport fault aborts the lease and maps to import_error."""
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
        put_client=_PutClient(fail=True),
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "import_error"
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)
    assert not any(p.endswith("/HttpNfcLeaseComplete") for p, _ in conn.vmomi_calls)


async def test_a_disk_that_fails_to_prepare_aborts_the_lease() -> None:
    """A source error after ImportVApp (a RuntimeError) aborts the lease like an upload fault."""

    class _UnpreparedDisk(_FakeDisk):
        async def __aenter__(self) -> _FakeDisk:
            raise LibraryDownloadError("download-session file 'app-disk1.vmdk' failed to prepare")

    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _UnpreparedDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "import_error"
    assert "failed to prepare" in result.issues[-1]["message"]
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)
    assert not any(p.endswith("/HttpNfcLeaseComplete") for p, _ in conn.vmomi_calls)


async def test_progress_heartbeat_fires_during_a_slow_upload() -> None:
    """AC1/AC2: the lease heartbeat runs concurrently with a slow transfer.

    A disk whose stream sleeps between chunks makes the upload outlast several
    heartbeat intervals; the engine keeps sending HttpNfcLeaseProgress (keeping
    the lease alive) and refreshing the source, then sends a final 100%.
    """
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll()],
    )
    disk = _FakeDisk(b"0123456789abcdef", chunk_sleep=0.02, chunks=8)
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": disk})
    result = await _run(conn, source, heartbeat_interval=0.01)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    progress = [
        body["percent"] for path, body in conn.vmomi_calls if path.endswith("/HttpNfcLeaseProgress")
    ]
    assert progress, "no HttpNfcLeaseProgress heartbeat fired"
    assert progress[-1] == 100  # final progress before Complete
    assert source.keep_alives >= 1  # the source session was refreshed too


async def test_import_passes_control_plane_lease_certificate_to_upload(fake_upload_client) -> None:
    """The lease property response supplies expected material, not the upload peer."""
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(with_ssl_cert=True)],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert fake_upload_client == [(None, "-----BEGIN CERTIFICATE-----")]


async def test_upload_certificate_failure_aborts_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rejected(*args, **kwargs):
        raise ovf_transfer.DeviceThumbprintError("upload certificate does not match lease")

    monkeypatch.setattr(ovf_transfer, "_put_disk", rejected)
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(ssl_thumbprint="AA:BB")],
    )
    result = await _run(conn, _FakeSource("<Envelope/>", {}))
    assert result.status == "import_error"
    assert result.issues[0]["category"] == "security"
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)
    assert not any(p.endswith("/HttpNfcLeaseComplete") for p, _ in conn.vmomi_calls)
    assert conn._client.puts == []


# ---------------------------------------------------------------------------
# Composite handler wiring (vm.import_from_library)
# ---------------------------------------------------------------------------


class _RecordingSource:
    instances: ClassVar[list[_RecordingSource]] = []

    def __init__(self, **_kw: Any) -> None:
        self.closed = False
        _RecordingSource.instances.append(self)

    async def aclose(self) -> None:
        self.closed = True


async def _call_handler(
    monkeypatch: pytest.MonkeyPatch, *, resolved: Any, engine_result: Any, params: dict[str, Any]
) -> Any:
    _RecordingSource.instances = []
    monkeypatch.setattr(
        _write.library_download, "LibraryDownloadSource", _RecordingSource, raising=True
    )

    async def _fake_resolve(**_kw: Any) -> Any:
        return resolved

    async def _fake_engine(**_kw: Any) -> Any:
        return engine_result

    monkeypatch.setattr(_write, "_resolve_deploy_library_item", _fake_resolve)
    monkeypatch.setattr(_write, "import_ovf_from_source", _fake_engine)
    return await _write.vm_import_from_library_composite(
        operator=_Operator(),
        target=_Target(),
        params=params,
        connector=SimpleNamespace(impl_id="vmware-rest", version="9.0"),  # type: ignore[arg-type]
    )


async def test_handler_maps_imported_to_deployed_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    engine_result = LeaseImportResult(
        status="imported",
        vm_id="vm-9",
        resource_type="VirtualMachine",
        transfer=[{"path": "d.vmdk", "device_id": "disk1", "size_bytes": 8}],
    )
    envelope = await _call_handler(
        monkeypatch,
        resolved=("item-1", None),
        engine_result=engine_result,
        params={"resource_pool": "rp-1", "datastore": "ds-1"},
    )
    assert envelope["status"] == "deployed"
    assert envelope["vm_id"] == "vm-9"
    assert envelope["library_item_id"] == "item-1"
    assert envelope["transfer"][0]["device_id"] == "disk1"
    assert _RecordingSource.instances[0].closed is True  # source cleaned up


async def test_handler_maps_import_failed_to_deploy_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    engine_result = LeaseImportResult(
        status="import_failed",
        issues=[{"category": "descriptor", "severity": "error", "message": "x"}],
    )
    envelope = await _call_handler(
        monkeypatch,
        resolved=("item-1", None),
        engine_result=engine_result,
        params={"resource_pool": "rp-1", "datastore": "ds-1"},
    )
    assert envelope["status"] == "deploy_failed"
    assert envelope["vm_id"] is None


async def test_handler_returns_gate_operation_result(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = OperationResult(
        status="pending", op_id="vmware.composite.vm.import_from_library", duration_ms=0.0
    )
    result = await _call_handler(
        monkeypatch,
        resolved=("item-1", None),
        engine_result=parked,
        params={"resource_pool": "rp-1", "datastore": "ds-1"},
    )
    assert result is parked
    assert _RecordingSource.instances[0].closed is True


async def test_handler_returns_resolution_error(monkeypatch: pytest.MonkeyPatch) -> None:
    resolution_error = {"status": "ambiguous_item", "candidates": ["a", "b"]}
    envelope = await _call_handler(
        monkeypatch,
        resolved=(None, resolution_error),
        engine_result=None,
        params={"resource_pool": "rp-1", "datastore": "ds-1", "library_item_name": "app"},
    )
    assert envelope["status"] == "ambiguous_item"


# ---------------------------------------------------------------------------
# #4217 -- an approved power-on of an imported VM only powers it on
# ---------------------------------------------------------------------------


class _PowerConnector:
    """Records the direct write sub-calls of the power-on step."""

    impl_id = "vmware-rest"
    version = "9.0"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def mount_op_path(self, target: Any, path: str, operator: Any) -> str:
        return f"/api{path}"

    async def _post_json(
        self, target: Any, path: str, *, operator: Any, verb: str, **_kw: Any
    ) -> Any:
        self.calls.append((verb, path))
        return {}


def _record_gate(
    monkeypatch: pytest.MonkeyPatch, verdict: OperationResult | None
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _gate(**kwargs: Any) -> OperationResult | None:
        calls.append(kwargs)
        return verdict

    monkeypatch.setattr(_write, "enforce_subop_policy", _gate)
    return calls


async def test_import_power_on_park_saves_the_import_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waiting power-on saves the imported VM's envelope as its resume state (#4217)."""
    parked = OperationResult(
        status="awaiting_approval",
        op_id="POST:/vcenter/vm/{vm}/power?action=start",
        duration_ms=0.0,
        extras={"approval_request_id": "req-1"},
    )
    gate_calls = _record_gate(monkeypatch, parked)
    engine_result = LeaseImportResult(
        status="imported", vm_id="vm-9", resource_type="VirtualMachine"
    )
    envelope = await _call_handler(
        monkeypatch,
        resolved=("item-1", None),
        engine_result=engine_result,
        params={"resource_pool": "rp-1", "datastore": "ds-1", "power_on": True},
    )
    assert envelope["powered_on"] is False
    assert "waits for approval request req-1" in envelope["issues"][-1]["message"]
    (power_gate,) = gate_calls
    assert power_gate["after_main_effect"] is True
    saved = power_gate["resume_state"]["imported"]
    assert saved["vm_id"] == "vm-9"
    assert saved["library_item_id"] == "item-1"
    assert saved["powered_on"] is False


async def test_import_resume_of_a_power_on_only_powers_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved power-on starts the imported VM; no lookup and no import run (#4217)."""
    from meho_backplane.operations.composite import (
        CompositeResumeState,
        composite_resume_state_var,
    )

    async def _must_not_run(**_kw: Any) -> Any:
        raise AssertionError("the resume must not resolve or import again")

    monkeypatch.setattr(_write, "_resolve_deploy_library_item", _must_not_run)
    monkeypatch.setattr(_write, "import_ovf_from_source", _must_not_run)
    gate_calls = _record_gate(monkeypatch, None)
    imported = {
        "status": "deployed",
        "vm_id": "vm-9",
        "resource_type": "VirtualMachine",
        "library_item_id": "item-1",
        "powered_on": False,
        "issues": [],
        "transfer": [],
        "candidates": None,
    }
    connector = _PowerConnector()
    token = composite_resume_state_var.set(
        CompositeResumeState(
            op_id="vmware.composite.vm.import_from_library",
            state={"imported": imported},
            after_main_effect=True,
        )
    )
    try:
        envelope = await _write.vm_import_from_library_composite(
            operator=_Operator(),  # type: ignore[arg-type]
            target=_Target(),
            params={"resource_pool": "rp-1", "datastore": "ds-1", "power_on": True},
            connector=connector,  # type: ignore[arg-type]
        )
    finally:
        composite_resume_state_var.reset(token)
    assert envelope == {**imported, "powered_on": True}
    assert connector.calls == [("POST", "/api/vcenter/vm/vm-9/power?action=start")]
    assert [call["op_id"] for call in gate_calls] == ["POST:/vcenter/vm/{vm}/power?action=start"]


async def test_import_refuses_to_re_run_for_an_approved_power_on_without_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An approved power-on with no saved envelope stops before any call (#4217).

    Such an approval request was saved before #4217. Running the import
    from the start would import a second VM, so the re-run refuses at once.
    """
    from meho_backplane.operations.composite import composite_resume_scope_var

    async def _must_not_run(**_kw: Any) -> Any:
        raise AssertionError("the refused resume must not resolve or import")

    monkeypatch.setattr(_write, "_resolve_deploy_library_item", _must_not_run)
    monkeypatch.setattr(_write, "import_ovf_from_source", _must_not_run)
    gate_calls = _record_gate(monkeypatch, None)
    connector = _PowerConnector()
    power_op = "POST:/vcenter/vm/{vm}/power?action=start"
    token = composite_resume_scope_var.set((power_op, "deadbeef"))
    try:
        out = await _write.vm_import_from_library_composite(
            operator=_Operator(),  # type: ignore[arg-type]
            target=_Target(),
            params={"resource_pool": "rp-1", "datastore": "ds-1", "power_on": True},
            connector=connector,  # type: ignore[arg-type]
        )
    finally:
        composite_resume_scope_var.reset(token)
    assert isinstance(out, OperationResult)
    assert out.status == "error"
    assert out.op_id == "vmware.composite.vm.import_from_library"
    assert out.extras["error_code"] == "composite_resume_unsupported"
    assert out.extras["approved_op_id"] == power_op
    assert "nothing was imported" in (out.error or "")
    assert connector.calls == []
    assert gate_calls == []


async def test_invalid_lease_certificate_aborts_before_opening_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from meho_backplane.connectors.vmware_rest import ovf_upload_tls

    monkeypatch.setattr(ovf_transfer, "upload_client", ovf_upload_tls.upload_client)
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(with_ssl_cert=True)],
    )
    # No disk exists: opening the source would fail this test before a security result.
    result = await _run(conn, _FakeSource("<Envelope/>", {}))
    assert result.status == "import_error"
    assert result.issues[0]["category"] == "security"
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)
    assert conn._client.puts == []
