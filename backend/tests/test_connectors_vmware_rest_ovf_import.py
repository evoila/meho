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
import datetime
import hashlib
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from meho_backplane.connectors import OperationResult
from meho_backplane.connectors.vmware_rest import ovf_import, ovf_transfer
from meho_backplane.connectors.vmware_rest.composites import _write
from meho_backplane.connectors.vmware_rest.ovf_import import ImportPlacement, LeaseImportResult


def _colon_hex(digest: str) -> str:
    """Render a bare hex digest in vCenter's uppercase colon-separated form."""
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()


class _Target:
    def __init__(self, host: str = "vcenter.lab", name: str = "vc-1") -> None:
        self.host = host
        self.name = name


class _Operator:
    raw_jwt = "jwt"


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]


class _PutClient:
    """Fake pooled client: records PUTs and drains the streamed body."""

    def __init__(self, *, fail: bool = False) -> None:
        self.puts: list[dict[str, Any]] = []
        self._fail = fail

    async def put(self, url: str, *, content: Any, headers: dict[str, str], timeout: Any) -> _Resp:
        drained = 0
        async for chunk in content:
            drained += len(chunk)
        self.puts.append({"url": url, "headers": headers, "drained": drained})
        return _Resp(500 if self._fail else 200)


class _EngineConnector:
    """Recording stand-in for VmwareRestConnector on the engine path."""

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

    async def _http_client(self, target: Any) -> _PutClient:
        return self._client


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
    if with_ssl_cert:  # 9.0-only field the engine must ignore
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

    return await ovf_import.import_ovf_from_source(
        connector=connector,  # type: ignore[arg-type]
        target=_Target(),
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
    assert result.transfer == [{"path": "app-disk1.vmdk", "device_id": "disk1", "size_bytes": 8}]
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


async def test_import_is_version_agnostic_ignoring_9_0_only_device_fields() -> None:
    """AC5: a device URL carrying the 9.0-only sslCertificate still imports cleanly.

    The engine reads only the pre-9.0 key / importKey / url fields, so a lease
    from a pre-9.0 (VCF 5.x migration-source) target and a 9.0+ target both work.
    """
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


# ---------------------------------------------------------------------------
# Device-host certificate thumbprint pinning (#3284)
# ---------------------------------------------------------------------------


def test_thumbprint_normalization_is_colon_and_case_insensitive() -> None:
    """Colons, whitespace, and case are stripped before comparison."""
    assert ovf_transfer._normalize_thumbprint("A1:B2:C3:D4") == "a1b2c3d4"
    assert ovf_transfer._normalize_thumbprint("  a1:b2 ") == "a1b2"


def test_cert_thumbprint_selects_algorithm_by_expected_length() -> None:
    """A 40-char expectation hashes SHA-1; 64 hashes SHA-256 (vim convention)."""
    der = b"esxi-leaf-cert-der"
    assert ovf_transfer._cert_thumbprint(der, 40) == hashlib.sha1(der).hexdigest()
    assert ovf_transfer._cert_thumbprint(der, 64) == hashlib.sha256(der).hexdigest()
    # An unrecognised length falls back to SHA-1, whose digest cannot equal a
    # differently-sized expected value — the comparison then fails closed.
    assert ovf_transfer._cert_thumbprint(der, 12) == hashlib.sha1(der).hexdigest()


async def test_verify_matches_when_presented_cert_hashes_to_the_thumbprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A device cert whose SHA-1 matches the lease thumbprint verifies silently."""
    der = b"esxi-real-cert"
    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", lambda *_a: der)
    thumbprint = _colon_hex(hashlib.sha1(der).hexdigest())
    await ovf_transfer.verify_device_thumbprint("https://esxi.lab:443/nfc/x.vmdk", thumbprint)


async def test_verify_raises_on_thumbprint_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cert that does not hash to the lease thumbprint fails closed."""
    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", lambda *_a: b"attacker-cert")
    thumbprint = _colon_hex(hashlib.sha1(b"real-cert").hexdigest())
    with pytest.raises(ovf_transfer.DeviceThumbprintError):
        await ovf_transfer.verify_device_thumbprint("https://esxi.lab/nfc/x.vmdk", thumbprint)


@pytest.mark.parametrize("absent", ["", None])
async def test_verify_is_fail_open_on_absent_thumbprint(
    monkeypatch: pytest.MonkeyPatch, absent: str | None
) -> None:
    """An empty/None thumbprint skips the handshake (vim: 'Empty if ... not needed')."""
    handshakes: list[str] = []
    monkeypatch.setattr(
        ovf_transfer, "_fetch_peer_cert_der", lambda h, *_a: handshakes.append(h) or b"x"
    )
    await ovf_transfer.verify_device_thumbprint("https://esxi.lab/nfc/x.vmdk", absent)
    assert handshakes == []  # fail-open: no pin handshake was attempted


async def test_verify_fails_closed_when_handshake_cannot_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handshake that cannot obtain a cert to compare is a closed failure."""

    def _boom(*_a: Any) -> bytes:
        raise OSError("connection refused")

    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", _boom)
    with pytest.raises(ovf_transfer.DeviceThumbprintError):
        await ovf_transfer.verify_device_thumbprint("https://esxi.lab/nfc/x.vmdk", "AA:BB:CC")


async def test_thumbprint_mismatch_aborts_before_any_disk_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mismatch aborts the lease pre-stream — no PUT, no Complete, security issue."""
    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", lambda *_a: b"attacker-cert")
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(ssl_thumbprint=_colon_hex(hashlib.sha1(b"real").hexdigest()))],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "import_error"
    assert result.issues[-1]["category"] == "security"
    assert conn._client.puts == []  # fail closed: not a single byte streamed
    assert any(p.endswith("/HttpNfcLeaseAbort") for p, _ in conn.vmomi_calls)
    assert not any(p.endswith("/HttpNfcLeaseComplete") for p, _ in conn.vmomi_calls)


async def test_thumbprint_match_streams_disk_and_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A matching thumbprint permits the stream and the import completes."""
    der = b"esxi-real-cert"
    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", lambda *_a: der)
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(ssl_thumbprint=_colon_hex(hashlib.sha1(der).hexdigest()))],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert conn._client.puts[0]["url"] == "https://vcenter.lab/nfc/lease-1/disk-0.vmdk"


async def test_absent_thumbprint_streams_under_existing_tls_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail-open policy: an empty sslThumbprint imports without a pin handshake."""
    handshakes: list[str] = []
    monkeypatch.setattr(
        ovf_transfer, "_fetch_peer_cert_der", lambda h, *_a: handshakes.append(h) or b"x"
    )
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(ssl_thumbprint="")],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert handshakes == []


async def test_thumbprint_pin_uses_the_wildcard_substituted_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning composes with `*`-host substitution: it dials the concrete host."""
    der = b"esxi-real-cert"
    seen: dict[str, Any] = {}

    def _fake(host: str, port: int, _timeout: float) -> bytes:
        seen["host"], seen["port"] = host, port
        return der

    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", _fake)
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[_ready_poll(ssl_thumbprint=_colon_hex(hashlib.sha1(der).hexdigest()))],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert seen["host"] == "vcenter.lab"  # the substituted host, never the raw "*"


async def test_private_device_host_streams_when_thumbprint_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning — not a host allowlist — authorizes a private ESXi device host.

    The per-target SSRF host screen stays off for device-URL PUTs (it would
    wrongly block a legitimate private ESXi host); a matching thumbprint is
    the replacement control, so a private-IP device host streams cleanly.
    """
    der = b"esxi-private-cert"
    monkeypatch.setattr(ovf_transfer, "_fetch_peer_cert_der", lambda *_a: der)
    conn = _EngineConnector(
        service_content=_service_content(),
        import_spec_result=_import_spec_result(),
        lease_ref=_lease_ref(),
        lease_polls=[
            _ready_poll(
                ssl_thumbprint=_colon_hex(hashlib.sha1(der).hexdigest()),
                url="https://10.11.16.9/nfc/lease-1/disk-0.vmdk",
            )
        ],
    )
    source = _FakeSource("<Envelope/>", {"app-disk1.vmdk": _FakeDisk(b"12345678")})
    result = await _run(conn, source)
    assert isinstance(result, LeaseImportResult)
    assert result.status == "imported"
    assert conn._client.puts[0]["url"] == "https://10.11.16.9/nfc/lease-1/disk-0.vmdk"


# ---------------------------------------------------------------------------
# The verification-off handshake itself (SonarCloud S5527/S4830 by-design, #3344)
# ---------------------------------------------------------------------------
#
# Every pin test above stubs ``_fetch_peer_cert_der``; these exercise the real
# ``check_hostname = False`` / ``CERT_NONE`` handshake the module docstring
# documents, against a loopback self-signed server. That is exactly what S5527
# (server-hostname-verification-disabled) and S4830 (cert-not-verified) flag: an
# ESXi device host serves a self-signed cert, so chain/hostname validation cannot
# be the control and the certificate *thumbprint* is. The suppression is both
# necessary (a verifying context refuses the endpoint) and safe (the pin fails
# closed on a mismatch) -- proven here end to end, no monkeypatch.


def _self_signed_pem() -> tuple[bytes, bytes, bytes]:
    """Return ``(cert_pem, key_pem, cert_der)`` for a throwaway self-signed cert."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "esxi-device.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key_pem,
        cert.public_bytes(serialization.Encoding.DER),
    )


@contextlib.contextmanager
def _self_signed_tls_server(tmp_path: Path, cert_pem: bytes, key_pem: bytes) -> Iterator[int]:
    """Serve *cert_pem* on a loopback port in a daemon thread; yield the port."""
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert_pem)
    key_file.write_bytes(key_pem)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_file), str(key_file))
    sock = socket.socket()
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(5)
    sock.settimeout(0.5)
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            try:
                client, _ = sock.accept()
            except (TimeoutError, OSError):
                continue
            try:
                tls = ctx.wrap_socket(client, server_side=True)
                tls.recv(64)
                tls.close()
            except (ssl.SSLError, OSError):
                with contextlib.suppress(OSError):
                    client.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield sock.getsockname()[1]
    finally:
        stop.set()
        thread.join(timeout=2)
        sock.close()


def test_fetch_peer_cert_der_returns_the_self_signed_cert_verification_rejects(
    tmp_path: Path,
) -> None:
    """The suppressed handshake fetches a self-signed cert a verifying context refuses.

    Exercises the S5527/S4830 by-design line over a real loopback handshake:
    ``_fetch_peer_cert_der`` returns the exact DER the self-signed server
    presents, while a default (verifying) context raises on the same endpoint --
    proving verification is off by necessity, with the thumbprint as the control.
    """
    cert_pem, key_pem, cert_der = _self_signed_pem()
    with _self_signed_tls_server(tmp_path, cert_pem, key_pem) as port:
        assert ovf_transfer._fetch_peer_cert_der("127.0.0.1", port, 5.0) == cert_der
        verifying = ssl.create_default_context()
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5.0) as raw,
            pytest.raises(ssl.SSLError),
        ):
            verifying.wrap_socket(raw, server_hostname="esxi-device.local")


async def test_pin_verifies_end_to_end_against_a_real_self_signed_handshake(
    tmp_path: Path,
) -> None:
    """``verify_device_thumbprint`` over the real handshake: match passes, mismatch fails closed.

    The pin tests above stub the handshake; this drives the real
    verification-off ``_fetch_peer_cert_der`` against a loopback self-signed
    server. The lease-attested SHA-1 of the presented cert verifies; a wrong
    thumbprint raises ``DeviceThumbprintError`` -- the security model intact
    without any hostname or chain check.
    """
    cert_pem, key_pem, cert_der = _self_signed_pem()
    good = _colon_hex(hashlib.sha1(cert_der).hexdigest())
    bad = _colon_hex(hashlib.sha1(b"not-this-cert").hexdigest())
    with _self_signed_tls_server(tmp_path, cert_pem, key_pem) as port:
        url = f"https://127.0.0.1:{port}/nfc/disk-0.vmdk"
        await ovf_transfer.verify_device_thumbprint(url, good)
        with pytest.raises(ovf_transfer.DeviceThumbprintError):
            await ovf_transfer.verify_device_thumbprint(url, bad)


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
        connector=object(),  # type: ignore[arg-type]
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
