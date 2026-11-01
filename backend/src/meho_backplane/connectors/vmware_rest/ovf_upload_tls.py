# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Check the lease certificate on the TLS connection that sends the disk.

HTTPcore calls ``start_tls`` before writing HTTP headers or body bytes.
Wrap that stream, and never allow a write before its certificate matches
vCenter's lease hash. Each new connection gets its own check. A dedicated
client carries no vCenter cookies, follows no redirects, and uses no proxy.
Before that, the device host from the lease passes the shared connect-time
address check, and the client dials only the checked IP address.
See https://www.encode.io/httpcore/network-backends/ for the stream API.
"""

from __future__ import annotations

import hashlib
import ssl
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors._shared.pinned_transport import build_pinned_async_transport
from meho_backplane.connectors.adapters.http import _build_ca_pinned_ssl_context


class DeviceThumbprintError(Exception):
    """The upload peer could not be authenticated from the lease."""


class _LeaseStream(httpcore.AsyncNetworkStream):
    def __init__(self, inner: httpcore.AsyncNetworkStream, expected: str) -> None:
        self._inner = inner
        self._expected = expected
        self._verified = False

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._inner = await self._inner.start_tls(
            ssl_context, server_hostname=server_hostname, timeout=timeout
        )
        try:
            peer = self._inner.get_extra_info("ssl_object")
            cert = peer.getpeercert(binary_form=True) if peer is not None else None
            if not cert:
                raise DeviceThumbprintError("upload peer presented no TLS certificate")
            algorithm = "sha1" if len(self._expected) == 40 else "sha256"
            # Legacy leases can use SHA-1. Respect provider security policy.
            try:
                actual = hashlib.new(algorithm, cert, usedforsecurity=True).hexdigest()
            except ValueError as exc:
                raise DeviceThumbprintError(
                    f"lease certificate hash {algorithm} is unavailable under crypto policy"
                ) from exc
            if actual != self._expected:
                raise DeviceThumbprintError("upload certificate does not match lease sslThumbprint")
            self._verified = True
            return self
        except BaseException:
            await self._inner.aclose()
            raise

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        if not self._verified:
            raise DeviceThumbprintError("refusing upload before certificate verification")
        await self._inner.write(buffer, timeout)

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return await self._inner.read(max_bytes, timeout)

    async def aclose(self) -> None:
        await self._inner.aclose()

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _LeaseBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend, expected: str) -> None:
        self._inner = inner
        self._expected = expected

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        stream = await self._inner.connect_tcp(
            host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
        )
        return _LeaseStream(stream, self._expected)


def upload_client(
    target: Any, thumbprint: str | None, certificate: str | None = None
) -> httpx.AsyncClient:
    """Use the lease's certificate hash instead of CA/hostname trust.

    ESXi can serve a self-signed certificate whose name differs from its
    upload URL. TLS still encrypts the connection; ``_LeaseStream`` checks
    identity before allowing even the HTTP headers to leave. No preflight
    connection or vCenter connection pool is used.
    """
    if certificate is not None:
        try:
            if not isinstance(certificate, str):
                raise ValueError("expected PEM text")
            certificates = x509.load_pem_x509_certificates(certificate.encode("ascii"))
            if len(certificates) != 1:
                raise ValueError("expected one lease certificate")
            der = certificates[0].public_bytes(serialization.Encoding.DER)
            thumbprint = hashlib.sha256(der, usedforsecurity=True).hexdigest()
        except ValueError as exc:
            raise DeviceThumbprintError("lease sslCertificate is invalid or unavailable") from exc
    if not thumbprint or not thumbprint.strip():
        ca_pin = getattr(target, "tls_ca_pin", None)
        if not getattr(target, "verify_tls", True) and not ca_pin:
            raise DeviceThumbprintError("upload requires a lease sslThumbprint or verified TLS")
        verify = _build_ca_pinned_ssl_context(ca_pin) if ca_pin else True
        transport = build_pinned_async_transport(screen_dial_addresses, verify=verify)
        return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
    expected = thumbprint.replace(":", "").replace(" ", "").strip().lower()
    if len(expected) not in (40, 64) or any(c not in "0123456789abcdef" for c in expected):
        raise DeviceThumbprintError(
            "lease sslThumbprint must be a SHA-1 or SHA-256 certificate hash"
        )
    transport = build_pinned_async_transport(screen_dial_addresses, verify=False)
    # Same network-backend seam as connectors/_shared/pinned_transport.py: the
    # lease check wraps the address-pinning backend, so the address is checked
    # and pinned before the TLS handshake starts.
    pool = transport._pool
    pool._network_backend = _LeaseBackend(pool._network_backend, expected)
    return httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
