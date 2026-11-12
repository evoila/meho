# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for the CA pin trust of ``net.http_probe``.

A local HTTPS server uses a certificate from a private test CA.

* Without a registered target that pins the CA, the probe reads
  ``tls_error`` and ``trust: default``.
* With one in the same tenant, the probe reads ``reachable: true`` and
  ``trust: pinned``, and names the target.
* A pin of another tenant, or of a deleted target, is never used.
* The pin is used for the first host only, not after a redirect.
"""

from __future__ import annotations

import asyncio
import datetime
import ipaddress
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import update

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.http_probe import (
    _NET_HTTP_PROBE_RESPONSE_SCHEMA,
    net_http_probe,
    register_net_http_probe_operations,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import Target
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings
from tests._targets_helpers import _insert_target

_TENANT = UUID(int=0)
_OTHER_TENANT = UUID(int=7)


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1,localhost")
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
async def _registered_op() -> AsyncIterator[None]:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    await register_net_http_probe_operations(embedding_service=service)
    yield


def _operator(tenant_id: UUID = _TENANT) -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=tenant_id,
        tenant_role=TenantRole.OPERATOR,
    )


@dataclass
class _Pki:
    ca_pem: str
    cert_path: Path
    key_path: Path


def _make_pki(tmp_path: Path) -> _Pki:
    """A private test CA and a leaf certificate for 127.0.0.1 and localhost."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Private CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "leaf")]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert_path = tmp_path / "leaf.pem"
    key_path = tmp_path / "leaf-key.pem"
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return _Pki(
        ca_pem=ca_cert.public_bytes(serialization.Encoding.PEM).decode(),
        cert_path=cert_path,
        key_path=key_path,
    )


async def _start_https(pki: _Pki, location: str | None = None) -> asyncio.AbstractServer:
    """HTTPS server on loopback: 200, or a 302 to *location* when given."""
    import ssl

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(pki.cert_path, pki.key_path)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (await reader.readline()) not in (b"\r\n", b"\n", b""):
                pass
            if location:
                head = f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n"
            else:
                head = "HTTP/1.1 200 OK\r\n"
            writer.write((head + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(_handle, "127.0.0.1", 0, ssl=ctx)


def _port(server: asyncio.AbstractServer) -> int:
    return int(server.sockets[0].getsockname()[1])


async def _stop(*servers: asyncio.AbstractServer) -> None:
    for server in servers:
        server.close()
        await server.wait_closed()


async def test_without_a_pin_the_private_ca_reads_tls_error(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    server = await _start_https(pki)
    try:
        result = await net_http_probe(
            _operator(), None, {"url": f"https://127.0.0.1:{_port(server)}/"}
        )
    finally:
        await _stop(server)
    assert result["reachable"] is False
    assert result["reason"] == "tls_error"
    assert result["trust"] == "default"
    assert result["trust_target"] is None


async def test_target_pin_makes_the_private_ca_reachable(
    tmp_path: Path, _registered_op: None
) -> None:
    pki = _make_pki(tmp_path)
    await _insert_target(
        tenant_id=_TENANT, name="demo-app", host="127.0.0.1", tls_ca_pin=pki.ca_pem
    )
    server = await _start_https(pki)
    try:
        result = await dispatch(
            operator=_operator(),
            connector_id="net-probe-1.x",
            op_id="net.http_probe",
            target=None,
            params={"url": f"https://127.0.0.1:{_port(server)}/"},
        )
    finally:
        await _stop(server)
    assert result.status == "ok", result.error
    body: dict[str, Any] = result.result
    assert body["reachable"] is True
    assert body["status"] == 200
    assert body["reason"] is None
    assert body["timing_ms"] is not None
    assert body["tls"] is not None
    assert body["trust"] == "pinned"
    assert body["trust_target"] == "demo-app"


async def test_fqdn_match_through_host_header_uses_the_pin(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    await _insert_target(
        tenant_id=_TENANT,
        name="by-fqdn",
        host="192.0.2.50",
        fqdn="LocalHost",
        tls_ca_pin=pki.ca_pem,
    )
    server = await _start_https(pki)
    try:
        result = await net_http_probe(
            _operator(),
            None,
            {"url": f"https://127.0.0.1:{_port(server)}/", "host_header": "localhost"},
        )
    finally:
        await _stop(server)
    assert result["reachable"] is True
    assert result["trust"] == "pinned"
    assert result["trust_target"] == "by-fqdn"


async def test_pin_of_another_tenant_is_never_used(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    await _insert_target(
        tenant_id=_OTHER_TENANT, name="other-tenant-app", host="127.0.0.1", tls_ca_pin=pki.ca_pem
    )
    server = await _start_https(pki)
    try:
        result = await net_http_probe(
            _operator(), None, {"url": f"https://127.0.0.1:{_port(server)}/"}
        )
    finally:
        await _stop(server)
    assert result["reason"] == "tls_error"
    assert result["trust"] == "default"
    assert result["trust_target"] is None


async def test_pin_of_a_deleted_target_is_never_used(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    row = await _insert_target(
        tenant_id=_TENANT, name="gone-app", host="127.0.0.1", tls_ca_pin=pki.ca_pem
    )
    async with get_sessionmaker()() as session:
        await session.execute(
            update(Target)
            .where(Target.id == row.id)
            .values(deleted_at=datetime.datetime.now(datetime.UTC))
        )
        await session.commit()
    server = await _start_https(pki)
    try:
        result = await net_http_probe(
            _operator(), None, {"url": f"https://127.0.0.1:{_port(server)}/"}
        )
    finally:
        await _stop(server)
    assert result["reason"] == "tls_error"
    assert result["trust"] == "default"


async def test_pin_is_not_used_after_a_redirect_to_another_host(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    await _insert_target(
        tenant_id=_TENANT, name="demo-app", host="127.0.0.1", tls_ca_pin=pki.ca_pem
    )
    second = await _start_https(pki)
    first = await _start_https(pki, location=f"https://localhost:{_port(second)}/")
    try:
        result = await net_http_probe(
            _operator(), None, {"url": f"https://127.0.0.1:{_port(first)}/"}
        )
    finally:
        await _stop(first, second)
    # The first host answered with the pin; the other host fails the default trust.
    assert result["reachable"] is False
    assert result["reason"] == "tls_error"
    assert result["trust"] == "pinned"


async def test_pin_is_used_for_a_redirect_to_the_same_host(tmp_path: Path) -> None:
    pki = _make_pki(tmp_path)
    await _insert_target(
        tenant_id=_TENANT, name="demo-app", host="127.0.0.1", tls_ca_pin=pki.ca_pem
    )
    second = await _start_https(pki)
    first = await _start_https(pki, location=f"https://127.0.0.1:{_port(second)}/")
    try:
        result = await net_http_probe(
            _operator(), None, {"url": f"https://127.0.0.1:{_port(first)}/"}
        )
    finally:
        await _stop(first, second)
    assert result["reachable"] is True
    assert result["status"] == 200
    assert result["trust"] == "pinned"


async def test_plain_http_probe_reports_default_trust_without_a_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no target lookup for a plain http probe")

    monkeypatch.setattr("meho_backplane.connectors.net.http_probe_trust.get_sessionmaker", _boom)
    result = await net_http_probe(_operator(), None, {"url": "http://127.0.0.1:9/"})
    assert result["trust"] == "default"
    assert result["trust_target"] is None


async def test_refused_host_does_not_look_up_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "localhost")

    async def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("no target lookup for a refused host")

    monkeypatch.setattr("meho_backplane.connectors.net.http_probe_trust.get_sessionmaker", _boom)
    from meho_backplane.connectors.net.allowlist import ProbeNotAllowedError

    with pytest.raises(ProbeNotAllowedError):
        await net_http_probe(_operator(), None, {"url": "https://192.0.2.9/"})


def test_trust_keys_are_in_the_response_schema() -> None:
    assert "trust" in _NET_HTTP_PROBE_RESPONSE_SCHEMA["properties"]
    assert "trust_target" in _NET_HTTP_PROBE_RESPONSE_SCHEMA["properties"]
    assert {"trust", "trust_target"} <= set(_NET_HTTP_PROBE_RESPONSE_SCHEMA["required"])
