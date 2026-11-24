# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Read the certificate a host presents, for the stale-pin error message.

When a target has a ``tls_ca_pin`` and TLS verification fails, the
operator needs to know what the host presents now. This module makes one
short handshake to the target's own host and port and reads the leaf
certificate. It sends no request and trusts nothing: the handshake does
not verify, because its only job is to show the certificate.

The destination goes through the same connect-time address check as the
connector connections first (:func:`screen_dial_addresses`), with the port
the probe dials. That check looks up DNS, so it runs in a worker thread,
inside the same timeout as the connect and the handshake. The probe dials
the checked address and offers the target name as the TLS server name. A
host name that does not resolve is refused, unless it is listed exactly on
the allowlist; then the probe dials that name. A refused destination is
not dialed.

It never raises. Any failure returns ``None`` and the error message then
says the certificate could not be read.
"""

from __future__ import annotations

import asyncio
import ssl
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.targets.ssrf_guard import _dialed_host

#: Seconds for the DNS lookup, the connect and the handshake together.
PROBE_TIMEOUT_S: float = 3.0
_DEFAULT_PORT: int = 443


def describe_certificate(der: bytes) -> dict[str, str]:
    """Return the issuer, subject and SHA-256 fingerprint of a DER certificate."""
    cert = x509.load_der_x509_certificate(der)
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return {
        "issuer": cert.issuer.rfc4514_string(),
        "subject": cert.subject.rfc4514_string(),
        "sha256": ":".join(digest[i : i + 2] for i in range(0, len(digest), 2)),
    }


async def probe_presented_leaf(
    host: str,
    port: int | None,
    server_name: str | None,
    timeout: float = PROBE_TIMEOUT_S,
) -> dict[str, str] | None:
    """Return the leaf certificate ``host`` presents now, or ``None``."""
    # Reading the certificate is the whole point, so this handshake must
    # not verify it. No data is sent over the connection.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    dial_port = port or _DEFAULT_PORT

    async def _screen_and_open() -> asyncio.StreamWriter:
        # Same check as the connector connections. Refused targets raise
        # here and are not dialed. The DNS lookup blocks, so it runs in a
        # worker thread. Dial the checked address, not the name. ``None``
        # means the exact name is on the allowlist: dial it by name.
        addrs = await asyncio.to_thread(
            screen_dial_addresses, host, dial_port, use_embedded_port=False
        )
        dial_host = addrs[0] if addrs else _dialed_host(host)
        _reader, opened = await asyncio.open_connection(
            dial_host,
            dial_port,
            ssl=ctx,
            server_hostname=server_name or host,
            ssl_handshake_timeout=timeout,
        )
        return opened

    writer: asyncio.StreamWriter | None = None
    try:
        # One timeout covers the DNS lookup, the connect and the handshake.
        writer = await asyncio.wait_for(_screen_and_open(), timeout=timeout)
        ssl_object: Any = writer.get_extra_info("ssl_object")
        der = ssl_object.getpeercert(binary_form=True) if ssl_object is not None else None
        return describe_certificate(der) if der else None
    except Exception:  # never raise out of the error path
        return None
    finally:
        if writer is not None:
            writer.close()
