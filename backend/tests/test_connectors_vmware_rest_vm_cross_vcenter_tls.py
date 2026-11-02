# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""The pinned client of ``vm.migrate_cross_vcenter`` checks every TLS connection.

Over an in-memory transport (httpcore's mock stream with a certificate per
connection): a connection that shows another certificate than the thumbprint
is closed right after the handshake, so no request byte, and no login,
reaches it. This holds for the second connection too, when the certificate
changes between the first read and the login. The real ``httpx`` client and
``httpcore`` pool are in the loop; only the socket is faked.
"""

from __future__ import annotations

import base64
import json
import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
import pytest
from httpcore._backends.mock import AsyncMockStream

from meho_backplane.connectors.vmware_rest import VmwareRestConnector
from meho_backplane.connectors.vmware_rest.composites import _vm_cross_vcenter_destination as dst
from tests._vmware_cross_vcenter_fake import (
    CERT,
    DEST_UUID,
    LOGIN,
    THUMBPRINT,
    dest_target,
    self_signed_der,
)
from tests._vmware_teardown_fake import operator

OTHER_CERT = self_signed_der("vc-b.example.test")
_THUMBPRINT = THUMBPRINT.replace(":", "").lower()
_VERSIONS = (
    '<namespaces version="1.0"><namespace><name>urn:vim25</name>'
    "<version>9.0.0.0</version></namespace></namespaces>"
)
_CONTENT = json.dumps({"_typeName": "ServiceContent", "about": {"instanceUuid": DEST_UUID}})


def _response(body: str, *, status: int = 200, close: bool = False) -> bytes:
    head = (
        f"HTTP/1.1 {status} OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    )
    if close:
        head += "Connection: close\r\n"
    return (head + "\r\n" + body).encode()


class _SslObject:
    def __init__(self, cert: bytes | None) -> None:
        self._cert = cert

    def getpeercert(self, binary_form: bool = False) -> bytes | None:
        return self._cert

    def selected_alpn_protocol(self) -> str:
        return "http/1.1"


class _CertStream(AsyncMockStream):
    """One in-memory connection: the certificate it shows and the bytes written to it."""

    def __init__(self, answers: list[bytes], cert: bytes | None) -> None:
        super().__init__(answers)
        self.cert = cert
        self.written: list[bytes] = []
        self.closed = False

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written.append(buffer)

    async def aclose(self) -> None:
        self.closed = True
        await super().aclose()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str) -> Any:
        return _SslObject(self.cert) if info == "ssl_object" else None


class _CertBackend(httpcore.AsyncNetworkBackend):
    """Each new connection takes the next ``(certificate, answers)`` of *connections*."""

    def __init__(self, connections: list[tuple[bytes | None, list[bytes]]]) -> None:
        self._pending = list(connections)
        self.streams: list[_CertStream] = []

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        cert, answers = self._pending.pop(0)
        stream = _CertStream(answers, cert)
        self.streams.append(stream)
        return stream

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise AssertionError("no unix socket here")

    async def sleep(self, seconds: float) -> None:
        return None


def _client(backend: _CertBackend) -> httpx.AsyncClient:
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool._network_backend = backend
    return httpx.AsyncClient(base_url="https://vc-b.example.test", transport=transport)


async def _stub_loader(_target: Any, _operator: Any) -> dict[str, str]:
    return dict(LOGIN)


async def _open(monkeypatch: pytest.MonkeyPatch, backend: _CertBackend) -> dst.DestinationSession:
    connector = VmwareRestConnector(session_loader=_stub_loader)
    client = _client(backend)

    async def _ephemeral(_target: Any) -> httpx.AsyncClient:
        return client

    monkeypatch.setattr(connector, "_ephemeral_http_client", _ephemeral)
    return await dst.open_destination(connector, dest_target(), operator(), _THUMBPRINT)


def _written(stream: _CertStream) -> bytes:
    return b"".join(stream.written)


async def test_a_connection_with_the_right_certificate_serves_the_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _CertBackend(
        [(CERT, [_response(_VERSIONS), _response('"tok-b"'), _response(_CONTENT)])]
    )
    session = await _open(monkeypatch, backend)
    assert session.instance_uuid == DEST_UUID
    assert session.login == LOGIN
    (stream,) = backend.streams
    basic = base64.b64encode(f"{LOGIN['username']}:{LOGIN['password']}".encode()).decode()
    assert f"Authorization: Basic {basic}".encode() in _written(stream)
    assert b"vmware-api-session-id: tok-b" in _written(stream)


async def test_a_certificate_that_changes_before_the_login_stops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first connection is right and ends; the login would open a second one."""
    backend = _CertBackend(
        [
            (CERT, [_response(_VERSIONS, close=True)]),
            (OTHER_CERT, [_response('"tok-b"')]),
        ]
    )
    with pytest.raises(dst.DestinationCertificateMismatchError) as raised:
        await _open(monkeypatch, backend)
    assert raised.value.shown == dst.certificate_thumbprint(OTHER_CERT)
    first, second = backend.streams
    assert b"GET /sdk/vimServiceVersions.xml" in _written(first)
    assert second.written == []
    assert second.closed is True
    assert LOGIN["password"].encode() not in _written(first)


async def test_a_wrong_certificate_on_the_first_connection_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _CertBackend([(OTHER_CERT, [_response(_VERSIONS)])])
    with pytest.raises(dst.DestinationCertificateMismatchError):
        await _open(monkeypatch, backend)
    (stream,) = backend.streams
    assert stream.written == []


@pytest.mark.parametrize("cert", [None, b"not-a-certificate"])
async def test_a_connection_without_a_certificate_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, cert: bytes | None
) -> None:
    backend = _CertBackend([(cert, [_response(_VERSIONS)])])
    with pytest.raises(dst.DestinationCertificateMismatchError) as raised:
        await _open(monkeypatch, backend)
    assert raised.value.shown is None
    assert backend.streams[0].written == []


async def test_a_write_before_the_handshake_is_refused() -> None:
    """A connection that never ran TLS (plain http) carries no byte."""
    inner = _CertStream([], CERT)
    stream = dst._ThumbprintStream(inner, _THUMBPRINT)
    with pytest.raises(dst.DestinationCertificateMismatchError):
        await stream.write(b"GET / HTTP/1.1")
    assert inner.written == []


async def test_a_refused_login_is_login_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _CertBackend([(CERT, [_response(_VERSIONS), _response("{}", status=401)])])
    with pytest.raises(dst.DestinationLoginFailedError, match="HTTP 401"):
        await _open(monkeypatch, backend)


def test_only_a_pool_based_client_can_be_pinned() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200)))
    with pytest.raises(TypeError):
        dst.pin_client_to_thumbprint(client, _THUMBPRINT)
