# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Exercise certificate checks on real TLS uploads, before headers or body."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import hashlib
import ipaddress
import ssl
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from meho_backplane.connectors.vmware_rest import ovf_transfer, ovf_upload_tls


def certificate(tmp_path: Path, name: str = "device") -> tuple[ssl.SSLContext, bytes, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "esxi-device.local")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM)
    cert_file = tmp_path / f"{name}.pem"
    key_file = tmp_path / f"{name}.key"
    cert_file.write_bytes(pem)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context, cert.public_bytes(serialization.Encoding.DER), pem.decode()


@contextlib.asynccontextmanager
async def server(context: ssl.SSLContext, *, redirect: str | None = None):
    received: list[bytes] = []
    tasks: set[asyncio.Task] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.current_task())
        raw = b""
        complete = False
        try:
            while b"\r\n\r\n" not in raw:
                chunk = await reader.read(4096)
                if not chunk:
                    return
                raw += chunk
            header, body = raw.split(b"\r\n\r\n", 1)
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            body += await reader.readexactly(length - len(body))
            received.append(header + b"\r\n\r\n" + body)
            complete = True
            response = (
                f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {redirect}\r\n"
                if redirect
                else "HTTP/1.1 200 OK\r\n"
            )
            writer.write((response + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
        finally:
            # Include partial HTTP headers as evidence if a rejected peer saw any.
            if raw and not complete:
                received.append(raw)
            writer.close()
            with contextlib.suppress(ConnectionError):
                await writer.wait_closed()
            tasks.discard(asyncio.current_task())

    listener = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    url = f"https://127.0.0.1:{listener.sockets[0].getsockname()[1]}/nfc/disk?ticket=secret"
    try:
        yield url, received
    finally:
        listener.close()
        await listener.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks), timeout=5)


async def put(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.put(url, content=b"disk-payload", headers={"Content-Length": "12"})


@pytest.mark.parametrize("verify_tls", [True, False])
@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
async def test_lease_hash_accepts_self_signed_upload(
    tmp_path: Path,
    verify_tls: bool,
    algorithm: str,
) -> None:
    context, der, _ = certificate(tmp_path)
    digest = hashlib.new(algorithm, der).hexdigest()
    thumbprint = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)).upper()
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(
            SimpleNamespace(verify_tls=verify_tls), thumbprint
        ) as client:
            assert (await put(client, url)).status_code == 200
        assert len(received) == 1
        assert received[0].endswith(b"disk-payload")
        assert b"Cookie:" not in received[0]
        assert b"Authorization:" not in received[0]


async def test_changed_certificate_is_rejected_before_headers_or_payload(tmp_path: Path) -> None:
    """Knowing the earlier certificate cannot authorize a different upload peer."""
    _, earlier_der, _ = certificate(tmp_path, "earlier")
    context, _, _ = certificate(tmp_path, "replacement")
    expected = hashlib.sha256(earlier_der).hexdigest()
    body_started = False

    async def payload():
        nonlocal body_started
        body_started = True
        yield b"disk-payload"

    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(
            SimpleNamespace(verify_tls=False), expected
        ) as client:
            with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="does not match"):
                await client.put(url, content=payload(), headers={"Content-Length": "12"})
        assert not body_started
        assert received == []


async def test_each_reconnection_checks_certificate(tmp_path: Path) -> None:
    context, der, _ = certificate(tmp_path)
    certificate(tmp_path, "replacement")
    expected = hashlib.sha256(der).hexdigest()
    async with server(context) as (first, received):
        async with ovf_upload_tls.upload_client(SimpleNamespace(), expected) as client:
            assert (await put(client, first)).status_code == 200
            # Change the certificate at the same host and port, after Connection: close.
            context.load_cert_chain(tmp_path / "replacement.pem", tmp_path / "replacement.key")
            with pytest.raises(ovf_upload_tls.DeviceThumbprintError):
                await put(client, first)
        assert len(received) == 1


@pytest.mark.parametrize("absent", [None, "", "  "])
async def test_missing_hash_requires_verified_tls(tmp_path: Path, absent: str | None) -> None:
    context, _, _ = certificate(tmp_path)
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="requires"):
        ovf_upload_tls.upload_client(SimpleNamespace(verify_tls=False), absent)
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(SimpleNamespace(verify_tls=True), absent) as client:
            with pytest.raises(httpx.ConnectError):
                await put(client, url)
        assert received == []


@pytest.mark.parametrize("verify_tls", [True, False])
async def test_missing_hash_accepts_pinned_ca(tmp_path: Path, verify_tls: bool) -> None:
    context, _, pem = certificate(tmp_path)
    async with server(context) as (url, received):
        target = SimpleNamespace(verify_tls=verify_tls, tls_ca_pin=pem)
        async with ovf_upload_tls.upload_client(target, None) as client:
            assert (await put(client, url)).status_code == 200
        assert len(received) == 1


async def test_missing_hash_keeps_hostname_checks(tmp_path: Path) -> None:
    context, _, pem = certificate(tmp_path)
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(SimpleNamespace(tls_ca_pin=pem), None) as client:
            with pytest.raises(httpx.ConnectError):
                await put(client, url.replace("127.0.0.1", "localhost"))
        assert received == []


async def test_redirect_is_not_followed(tmp_path: Path) -> None:
    context, der, _ = certificate(tmp_path)
    async with (
        server(context) as (destination, untouched),
        server(context, redirect=destination) as (url, received),
        ovf_upload_tls.upload_client(SimpleNamespace(), hashlib.sha256(der).hexdigest()) as client,
    ):
        response = await put(client, url)
        assert response.status_code == 307
        with pytest.raises(httpx.HTTPStatusError):
            response.raise_for_status()
        assert len(received) == 1
        assert untouched == []


@pytest.mark.parametrize("bad", ["abc", "x" * 40, ":" * 40])
def test_malformed_hash_is_rejected(bad: str) -> None:
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError):
        ovf_upload_tls.upload_client(SimpleNamespace(), bad)


@pytest.mark.parametrize("url", ["http://127.0.0.1/upload", "https:///upload"])
async def test_non_tls_device_url_is_rejected_before_opening_source(url: str) -> None:
    entry = ovf_transfer.TransferEntry("disk", "disk", url, 1, True)
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="HTTPS"):
        await ovf_transfer._upload_one(
            None,
            SimpleNamespace(),
            entry=entry,
            source=None,
            tracker=ovf_transfer.ProgressTracker(1),
        )


async def test_missing_hash_uses_default_trust_bundle(tmp_path: Path, monkeypatch) -> None:
    context, _, _ = certificate(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "device.pem"))
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(SimpleNamespace(verify_tls=True), None) as client:
            assert (await put(client, url)).status_code == 200
        assert len(received) == 1


async def test_unverified_stream_cannot_send_even_headers() -> None:
    stream = ovf_upload_tls._LeaseStream(None, "a" * 40)
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="before"):
        await stream.write(b"PUT /?ticket=secret HTTP/1.1\r\n")


async def test_missing_peer_certificate_closes_connection() -> None:
    class NoCertificate:
        closed = False

        async def start_tls(self, *args, **kwargs):
            return self

        def get_extra_info(self, key):
            return None

        async def aclose(self):
            self.closed = True

    inner = NoCertificate()
    stream = ovf_upload_tls._LeaseStream(inner, "a" * 40)
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="no TLS certificate"):
        await stream.start_tls(ssl.create_default_context())
    assert inner.closed


def test_plan_keeps_lease_hash_and_substitutes_private_device_host() -> None:
    plan = ovf_transfer.plan_transfers(
        [{"deviceId": "disk", "path": "disk.vmdk", "size": 12}],
        [
            {
                "importKey": "disk",
                "url": "https://*/nfc/disk",
                "disk": True,
                "sslThumbprint": "AA:BB",
            }
        ],
        "192.168.1.2",
    )
    assert plan[0].ssl_thumbprint == "AA:BB"
    assert plan[0].url == "https://192.168.1.2/nfc/disk"


@pytest.mark.parametrize("algorithm", ["sha1", "sha256"])
async def test_blocked_certificate_hash_fails_before_headers_or_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    context, der, _ = certificate(tmp_path)
    expected = hashlib.new(algorithm, der).hexdigest()
    original = hashlib.new
    body_started = False

    def policy_hash(name, data=b"", **kwargs):
        if name == algorithm:
            assert kwargs.get("usedforsecurity", True) is True
            raise ValueError("hash blocked by provider policy")
        return original(name, data, **kwargs)

    async def payload():
        nonlocal body_started
        body_started = True
        yield b"disk-payload"

    monkeypatch.setattr(hashlib, "new", policy_hash)
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(
            SimpleNamespace(verify_tls=False), expected
        ) as client:
            with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="crypto policy"):
                await client.put(url, content=payload(), headers={"Content-Length": "12"})
        assert not body_started
        assert received == []


async def test_lease_pem_preferred_to_legacy_pin_without_sha1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context, _, pem = certificate(tmp_path)
    original = hashlib.new

    def no_sha1(name, data=b"", **kwargs):
        if name == "sha1":
            raise AssertionError("stronger lease material must not use SHA-1")
        return original(name, data, **kwargs)

    monkeypatch.setattr(hashlib, "new", no_sha1)
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(
            SimpleNamespace(verify_tls=False), "a" * 40, pem
        ) as client:
            assert (await put(client, url)).status_code == 200
        assert len(received) == 1


async def test_lease_pem_rejects_different_upload_certificate(tmp_path: Path) -> None:
    _, _, expected_pem = certificate(tmp_path, "expected")
    context, _, _ = certificate(tmp_path, "replacement")
    async with server(context) as (url, received):
        async with ovf_upload_tls.upload_client(
            SimpleNamespace(verify_tls=False), None, expected_pem
        ) as client:
            with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="does not match"):
                await put(client, url)
        assert received == []


@pytest.mark.parametrize("pem", ["", "not PEM", "-----BEGIN CERTIFICATE-----", 7])
async def test_invalid_supplied_lease_pem_fails_before_source_open(pem) -> None:
    entry = ovf_transfer.TransferEntry(
        "disk", "disk", "https://127.0.0.1/upload", 1, True, "a" * 40, pem
    )
    with pytest.raises(ovf_upload_tls.DeviceThumbprintError, match="sslCertificate"):
        await ovf_transfer._upload_one(
            None,
            SimpleNamespace(verify_tls=False),
            entry=entry,
            source=None,
            tracker=ovf_transfer.ProgressTracker(1),
        )


def test_plan_preserves_supplied_lease_certificate() -> None:
    plan = ovf_transfer.plan_transfers(
        [{"deviceId": "disk", "path": "disk.vmdk", "size": 12}],
        [
            {
                "importKey": "disk",
                "url": "https://*/nfc/disk",
                "disk": True,
                "sslThumbprint": "a" * 40,
                "sslCertificate": "lease PEM",
            }
        ],
        "192.168.1.2",
    )
    assert plan[0].ssl_certificate == "lease PEM"
    assert plan[0].ssl_thumbprint == "a" * 40
