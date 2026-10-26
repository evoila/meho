# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Capture or block the HTTP requests the LLM SDKs send through ``httpx2``.

The ``anthropic`` 1.x and ``openai`` 3.x SDKs send with ``httpx2``, not
``httpx``. ``respx`` patches ``httpx`` only, so it does not see these
requests. The helpers here patch ``httpx2.AsyncHTTPTransport`` instead.
The real SDK still builds each request, so a test reads exactly what
would go on the wire. No network is used.

* :func:`capture_httpx2_requests` answers every request with a fake
  reply and records the request.
* :func:`no_httpx2_egress` fails the test when any ``httpx2`` request
  is sent inside the block. Use it next to ``respx`` (which covers
  plain ``httpx``) to prove that no HTTP call leaves the process.
* :func:`write_test_ca`, :func:`client_ssl_context` and
  :func:`ca_common_names` check which CA
  certificates an SDK client trusts (the ``SSL_CERT_FILE`` case).
"""

from __future__ import annotations

import datetime
import ssl
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx2
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

#: A minimal non-streamed Chat Completions reply: one text message.
OPENAI_TEXT_REPLY: dict[str, Any] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "created": 0,
    "model": "test-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}

#: Builds the fake reply for one captured request.
Responder = Callable[[httpx2.Request], httpx2.Response]


def capture_httpx2_requests(
    monkeypatch: pytest.MonkeyPatch, respond: Responder
) -> list[httpx2.Request]:
    """Answer every async ``httpx2`` request with ``respond`` and record it.

    Returns the list the requests are added to, in send order. Each
    request body is read before it is recorded, so ``request.content``
    works in the test.
    """
    sent: list[httpx2.Request] = []

    async def _fake_send(
        self: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        await request.aread()
        sent.append(request)
        return respond(request)

    monkeypatch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _fake_send)
    return sent


@contextmanager
def no_httpx2_egress() -> Iterator[list[str]]:
    """Fail the test if any ``httpx2`` request is sent inside the block.

    Each request is recorded and then refused with a connect error, so
    it never reaches the network. The check runs when the block ends,
    not when the request is sent. An SDK retries and wraps transport
    errors, so an error raised at send time alone could be swallowed.
    Yields the list of refused requests (``"METHOD URL"``).
    """
    refused: list[str] = []

    def _refuse(request: httpx2.Request) -> httpx2.ConnectError:
        refused.append(f"{request.method} {request.url}")
        return httpx2.ConnectError("blocked by the zero-egress guard", request=request)

    async def _refuse_async(
        self: httpx2.AsyncHTTPTransport, request: httpx2.Request
    ) -> httpx2.Response:
        raise _refuse(request)

    def _refuse_sync(self: httpx2.HTTPTransport, request: httpx2.Request) -> httpx2.Response:
        raise _refuse(request)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(httpx2.AsyncHTTPTransport, "handle_async_request", _refuse_async)
        patch.setattr(httpx2.HTTPTransport, "handle_request", _refuse_sync)
        yield refused
    assert not refused, f"httpx2 request(s) sent inside the zero-egress guard: {refused}"


def write_test_ca(directory: Path, common_name: str = "MEHO test CA") -> Path:
    """Write a new self-signed CA certificate (PEM) and return its path."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = directory / "test-ca.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def client_ssl_context(client: httpx2.AsyncClient) -> ssl.SSLContext:
    """Return the SSL context ``client`` uses for its connections.

    ``httpx2`` builds the context when the client is created and has no
    public getter for it. So this reads it from the default transport's
    connection pool. If an ``httpx2`` upgrade moves it, this helper fails
    loudly instead of passing.
    """
    ssl_context: ssl.SSLContext = client._transport._pool._ssl_context  # type: ignore[attr-defined]
    return ssl_context


def ca_common_names(ssl_context: ssl.SSLContext) -> set[str]:
    """Return the common names of the CA certificates loaded into an SSL context.

    A ``truststore`` context (what ``httpx2`` builds on macOS and Windows,
    and on Linux before ``httpx2`` 2.15) leaves certificate checks to the
    operating system store and loads no CA file of its own. It cannot list
    its CAs and raises ``NotImplementedError``. The helper returns an empty
    set for it, so a test CA is never reported as trusted there.
    """
    try:
        ca_certs = ssl_context.get_ca_certs()
    except NotImplementedError:
        return set()
    names: set[str] = set()
    for cert in ca_certs:
        for rdn in cert["subject"]:
            for key, value in rdn:
                if key == "commonName":
                    names.add(value)
    return names
