# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The presented-certificate probe used by the stale-pin TLS error (#3867)."""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
import ssl
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from meho_backplane.connectors._shared import dial_guard
from meho_backplane.operations._tls_presented import describe_certificate, probe_presented_leaf

# The suite lets a name that does not resolve be dialed by name (conftest).
# The tests below that need the real refusal put it back.
_REAL_UNRESOLVED = dial_guard._unresolved


def _make_cert() -> tuple[x509.Certificate, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Example Test CA")])
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "host.example.test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert, key


@pytest.fixture
async def tls_server(tmp_path: Path) -> AsyncIterator[tuple[int, x509.Certificate]]:
    cert, key = _make_cert()
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_file, key_file)

    async def _handle(_r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ctx)
    port = server.sockets[0].getsockname()[1]
    try:
        yield port, cert
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_reads_issuer_subject_and_sha256(
    tls_server: tuple[int, x509.Certificate],
) -> None:
    port, cert = tls_server
    out = await probe_presented_leaf("127.0.0.1", port, "host.example.test")

    assert out == describe_certificate(cert.public_bytes(serialization.Encoding.DER))
    assert out is not None
    assert out["issuer"] == "CN=Example Test CA"
    assert out["subject"] == "CN=host.example.test"
    assert out["sha256"] == ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA256()))


@pytest.mark.asyncio
async def test_probe_returns_none_when_nothing_listens() -> None:
    server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()

    assert await probe_presented_leaf("127.0.0.1", port, None, timeout=1.0) is None


@pytest.mark.asyncio
async def test_probe_returns_none_when_the_host_is_not_tls() -> None:
    async def _plain(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        w.write(b"hello\n")
        await w.drain()
        w.close()

    server = await asyncio.start_server(_plain, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        assert await probe_presented_leaf("127.0.0.1", port, None, timeout=1.0) is None
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_probe_does_not_dial_a_refused_destination(
    tls_server: tuple[int, x509.Certificate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port, _cert = tls_server
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
    dialed: list[object] = []

    async def _spy(*args: object, **kwargs: object) -> None:
        dialed.append(args)
        raise AssertionError("must not dial")

    monkeypatch.setattr("asyncio.open_connection", _spy)

    assert await probe_presented_leaf("127.0.0.1", port, "host.example.test") is None
    assert dialed == []


@pytest.mark.asyncio
async def test_probe_dials_the_screened_address_with_the_sni_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    async def _fake(host: str, port: int, **kwargs: object) -> None:
        seen["host"] = host
        seen["server_hostname"] = kwargs["server_hostname"]
        raise OSError("stop")

    monkeypatch.setattr(
        "meho_backplane.operations._tls_presented.screen_dial_addresses",
        lambda h, p, **kw: ["192.0.2.7"],
    )
    monkeypatch.setattr("asyncio.open_connection", _fake)

    assert await probe_presented_leaf("host.example.test", 8443, None) is None
    assert seen == {"host": "192.0.2.7", "server_hostname": "host.example.test"}


@pytest.mark.asyncio
async def test_slow_dns_stays_within_the_timeout_and_does_not_block_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _slow_screen(host: str, port: int | None, **kwargs: object) -> list[str]:
        time.sleep(0.5)
        return ["192.0.2.7"]

    async def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not dial after the timeout")

    monkeypatch.setattr(
        "meho_backplane.operations._tls_presented.screen_dial_addresses", _slow_screen
    )
    monkeypatch.setattr("asyncio.open_connection", _never)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    loop = asyncio.get_running_loop()
    start = loop.time()
    try:
        out = await probe_presented_leaf("host.example.test", 443, None, timeout=0.1)
    finally:
        ticker.cancel()
    elapsed = loop.time() - start

    assert out is None
    assert elapsed < 0.4
    # The loop kept running other tasks while DNS was slow.
    assert ticks >= 3


@pytest.mark.asyncio
async def test_probe_does_not_dial_a_name_that_did_not_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name does not resolve at the check, and would resolve to 127.0.0.1 later."""
    monkeypatch.delenv("MEHO_TARGET_SSRF_ALLOWLIST", raising=False)
    monkeypatch.setattr(dial_guard, "_unresolved", _REAL_UNRESOLVED)
    answers: list[list[ipaddress.IPv4Address]] = [[], [ipaddress.IPv4Address("127.0.0.1")]]
    monkeypatch.setattr(
        "meho_backplane.targets.ssrf_guard._resolve_addrs",
        lambda host: answers.pop(0) if answers else [ipaddress.IPv4Address("127.0.0.1")],
    )
    dialed: list[object] = []

    async def _spy(*args: object, **kwargs: object) -> None:
        dialed.append(args)
        raise AssertionError("must not dial")

    monkeypatch.setattr("asyncio.open_connection", _spy)

    assert await probe_presented_leaf("rebind.example.test", 443, None) is None
    assert dialed == []


@pytest.mark.asyncio
async def test_probe_dials_a_name_listed_exactly_on_the_allowlist(
    tls_server: tuple[int, x509.Certificate],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``localhost`` does not resolve through the check here, but it is listed."""
    port, cert = tls_server
    monkeypatch.setenv("MEHO_TARGET_SSRF_ALLOWLIST", "localhost")
    monkeypatch.setattr(dial_guard, "_unresolved", _REAL_UNRESOLVED)
    monkeypatch.setattr("meho_backplane.targets.ssrf_guard._resolve_addrs", lambda host: [])
    real_open = asyncio.open_connection
    dialed: list[str] = []

    async def _record(host: str, port: int, **kwargs: object) -> object:
        dialed.append(host)
        return await real_open(host, port, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("asyncio.open_connection", _record)

    out = await probe_presented_leaf("localhost", port, "host.example.test")

    assert dialed == ["localhost"]
    assert out == describe_certificate(cert.public_bytes(serialization.Encoding.DER))
