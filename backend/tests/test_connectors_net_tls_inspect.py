# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Tests for ``net.tls_inspect`` — full presented chain (#2407, Initiative #2405).

Covers T2 of the net.* family on the T1 keystone (#2406):

* The **full presented chain** (leaf → intermediates → root-if-sent) is
  returned leaf-first for a multi-cert endpoint, targetless, fresh boot.
* ``hostname_match`` is computed independently of the (disabled) stack
  verification — pinned true for a SAN match and false for a mismatch.
* A self-signed / expired / mismatched cert is **inspected**
  (``handshake=true``, ``status="ok"``), never rejected;
  ``chain_complete`` reflects whether a self-signed root was presented.
* ``host`` is probe-allowlist-gated (T1 foundation) and the audit row
  records the literal host:port.
* Failed handshakes (refused / timeout / DNS / non-TLS) return
  ``handshake=false`` with ``status="ok"`` — the return-failures contract.

The TLS server runs in a background thread serving a real cert chain
built with ``cryptography``; the handler dials it over loopback (which
the tests add to ``MEHO_NETDIAG_PROBE_ALLOWLIST``).
"""

from __future__ import annotations

import contextlib
import datetime
import socket
import ssl
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from sqlalchemy import select

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.connectors.net import tls as net_tls
from meho_backplane.connectors.net.allowlist import PROBE_ALLOWLIST_ENV
from meho_backplane.connectors.net.tls import (
    net_tls_inspect,
    register_net_tls_inspect_operation,
)
from meho_backplane.connectors.schemas import OperationResult
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog, EndpointDescriptor
from meho_backplane.operations import dispatch, reset_dispatcher_caches
from meho_backplane.settings import get_settings

_CONNECTOR_ID = "net-probe-1.x"
_OP_ID = "net.tls_inspect"
_LEAF_CN = "appliance.local"


# ---------------------------------------------------------------------------
# Settings env + dispatcher isolation (mirrors the T1 test module)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("KEYCLOAK_ISSUER_URL", "https://keycloak.test/realms/meho")
    monkeypatch.setenv("KEYCLOAK_AUDIENCE", "meho-backplane")
    monkeypatch.delenv(PROBE_ALLOWLIST_ENV, raising=False)
    get_settings.cache_clear()
    reset_dispatcher_caches()
    yield
    get_settings.cache_clear()
    reset_dispatcher_caches()


@pytest.fixture
def stub_embedding_service() -> AsyncMock:
    service = AsyncMock()
    service.encode_one.return_value = [0.1] * 384
    service.encode.return_value = [[0.1] * 384]
    service.dimension = 384
    return service


@pytest.fixture
async def _registered_tls_inspect_op(
    stub_embedding_service: AsyncMock,
) -> AsyncIterator[None]:
    await register_net_tls_inspect_operation(embedding_service=stub_embedding_service)
    yield


def _make_operator() -> Operator:
    return Operator(
        sub="test-operator",
        name=None,
        email=None,
        raw_jwt="fake.jwt.value",
        tenant_id=UUID(int=0),
        tenant_role=TenantRole.OPERATOR,
    )


async def _dispatch_inspect(params: dict[str, Any]) -> OperationResult:
    return await dispatch(
        operator=_make_operator(),
        connector_id=_CONNECTOR_ID,
        op_id=_OP_ID,
        target=None,
        params=params,
    )


# ---------------------------------------------------------------------------
# Certificate + TLS-server test doubles
# ---------------------------------------------------------------------------

_NOW = datetime.datetime.now(datetime.UTC)


def _new_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _sign(
    subject_cn: str,
    subject_key: rsa.RSAPrivateKey,
    issuer_name: x509.Name | None,
    issuer_key: rsa.RSAPrivateKey,
    *,
    san_dns: list[str] | None = None,
    is_ca: bool = False,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> x509.Certificate:
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject_cn)])
    # ``issuer_name is None`` ⇒ a self-signed cert (issuer == subject), so
    # ``subject == issuer`` holds and the handler flags it self_signed.
    if issuer_name is None:
        issuer_name = subject
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer_name)
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before or (_NOW - datetime.timedelta(days=1)))
        .not_valid_after(not_after or (_NOW + datetime.timedelta(days=365)))
    )
    if is_ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
    if san_dns:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in san_dns]), False
        )
    return builder.sign(issuer_key, hashes.SHA256())


def _pem(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


class _TLSServer:
    """A loopback TLS server that presents a fixed cert chain to each client.

    Serves in a daemon thread: accept → TLS handshake → drain a few bytes
    → close, looping until :meth:`stop`. ``chain_pem`` is the concatenated
    leaf-first PEM bundle the server presents; the ordering is exactly what
    ``get_peer_cert_chain`` reads back.
    """

    def __init__(self, tmp_path: Path, chain_pem: bytes, key_pem: bytes) -> None:
        cert_file = tmp_path / "chain.pem"
        key_file = tmp_path / "key.pem"
        cert_file.write_bytes(chain_pem)
        key_file.write_bytes(key_pem)
        self._ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ctx.load_cert_chain(str(cert_file), str(key_file))
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(5)
        self._sock.settimeout(0.5)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            try:
                tls = self._ctx.wrap_socket(client, server_side=True)
                tls.recv(64)
                tls.close()
            except (ssl.SSLError, OSError):
                with contextlib.suppress(OSError):
                    client.close()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


@pytest.fixture
def multi_cert_server(tmp_path: Path) -> Iterator[_TLSServer]:
    """A server presenting a full 3-cert chain: leaf → intermediate → root."""
    root_key = _new_key()
    root = _sign("Test Root CA", root_key, None, root_key, is_ca=True)
    root_name = root.subject
    int_key = _new_key()
    intermediate = _sign("Test Intermediate CA", int_key, root_name, root_key, is_ca=True)
    leaf_key = _new_key()
    leaf = _sign(
        _LEAF_CN, leaf_key, intermediate.subject, int_key, san_dns=[_LEAF_CN, "*.wild.local"]
    )
    bundle = _pem(leaf) + _pem(intermediate) + _pem(root)
    server = _TLSServer(tmp_path, bundle, _key_pem(leaf_key))
    try:
        yield server
    finally:
        server.stop()


def _self_signed_server(
    tmp_path: Path,
    *,
    san_dns: list[str] | None = None,
    not_before: datetime.datetime | None = None,
    not_after: datetime.datetime | None = None,
) -> _TLSServer:
    key = _new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _LEAF_CN)])
    cert = _sign(
        _LEAF_CN,
        key,
        name,
        key,
        not_before=not_before,
        san_dns=san_dns if san_dns is not None else [_LEAF_CN],
        not_after=not_after,
    )
    return _TLSServer(tmp_path, _pem(cert), _key_pem(key))


# ---------------------------------------------------------------------------
# Full chain, leaf-first, targetless, fresh boot
# ---------------------------------------------------------------------------


async def test_returns_full_chain_leaf_first(
    monkeypatch: pytest.MonkeyPatch,
    multi_cert_server: _TLSServer,
    _registered_tls_inspect_op: None,
) -> None:
    """A multi-cert endpoint yields leaf → intermediate → root, leaf-first."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch_inspect(
        {"host": "127.0.0.1", "port": multi_cert_server.port, "server_name": _LEAF_CN}
    )
    assert result.status == "ok", result.error
    body = result.result
    assert body["handshake"] is True
    assert body["reason"] is None
    chain = body["chain"]
    assert len(chain) == 3
    # Leaf-first ordering.
    assert chain[0]["subject"] == "CN=appliance.local"
    assert chain[1]["subject"] == "CN=Test Intermediate CA"
    assert chain[2]["subject"] == "CN=Test Root CA"
    # leaf is the convenience alias for chain[0].
    assert body["leaf"] == chain[0]
    assert body["not_after"] == chain[0]["not_after"]
    # The root the server sent is self-signed → chain terminates complete.
    assert chain[2]["self_signed"] is True
    assert chain[0]["self_signed"] is False
    assert body["chain_complete"] is True
    assert body["protocol"].startswith("TLS")
    assert body["cipher"]


# ---------------------------------------------------------------------------
# hostname_match — both ways, independent of disabled verification
# ---------------------------------------------------------------------------


async def test_hostname_match_true_for_san_match(
    monkeypatch: pytest.MonkeyPatch,
    multi_cert_server: _TLSServer,
    _registered_tls_inspect_op: None,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch_inspect(
        {"host": "127.0.0.1", "port": multi_cert_server.port, "server_name": _LEAF_CN}
    )
    assert result.status == "ok", result.error
    assert result.result["hostname_match"] is True


async def test_hostname_match_false_for_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    multi_cert_server: _TLSServer,
    _registered_tls_inspect_op: None,
) -> None:
    """A non-matching server_name → hostname_match False, cert still inspected."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch_inspect(
        {"host": "127.0.0.1", "port": multi_cert_server.port, "server_name": "wrong.example.com"}
    )
    assert result.status == "ok", result.error
    body = result.result
    assert body["handshake"] is True  # verification is off — still inspected
    assert body["hostname_match"] is False
    assert len(body["chain"]) == 3


async def test_hostname_match_true_for_wildcard_san(
    monkeypatch: pytest.MonkeyPatch,
    multi_cert_server: _TLSServer,
    _registered_tls_inspect_op: None,
) -> None:
    """A ``*.wild.local`` SAN matches one leftmost label."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    result = await _dispatch_inspect(
        {"host": "127.0.0.1", "port": multi_cert_server.port, "server_name": "node1.wild.local"}
    )
    assert result.status == "ok", result.error
    assert result.result["hostname_match"] is True


# ---------------------------------------------------------------------------
# Self-signed / expired inspected, never rejected; chain_complete signal
# ---------------------------------------------------------------------------


async def test_self_signed_cert_is_inspected_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_tls_inspect_op: None,
) -> None:
    """A self-signed leaf handshakes, reports self_signed + chain_complete."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    server = _self_signed_server(tmp_path)
    try:
        result = await _dispatch_inspect(
            {"host": "127.0.0.1", "port": server.port, "server_name": _LEAF_CN}
        )
    finally:
        server.stop()
    assert result.status == "ok", result.error
    body = result.result
    assert body["handshake"] is True
    assert body["reason"] is None
    assert len(body["chain"]) == 1
    assert body["chain"][0]["self_signed"] is True
    assert body["chain_complete"] is True
    assert body["hostname_match"] is True


async def test_expired_cert_is_inspected_not_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_tls_inspect_op: None,
) -> None:
    """An expired cert completes the handshake and reports a past not_after."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    past = _NOW - datetime.timedelta(days=10)
    server = _self_signed_server(
        tmp_path, not_before=_NOW - datetime.timedelta(days=400), not_after=past
    )
    try:
        result = await _dispatch_inspect(
            {"host": "127.0.0.1", "port": server.port, "server_name": _LEAF_CN}
        )
    finally:
        server.stop()
    assert result.status == "ok", result.error
    body = result.result
    assert body["handshake"] is True
    leaf_not_after = datetime.datetime.fromisoformat(body["not_after"])
    assert leaf_not_after < _NOW


async def test_chain_complete_false_when_root_not_sent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_tls_inspect_op: None,
) -> None:
    """A leaf + intermediate (no root) presents an incomplete chain."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    root_key = _new_key()
    root = _sign("Test Root CA", root_key, None, root_key, is_ca=True)
    int_key = _new_key()
    intermediate = _sign("Test Intermediate CA", int_key, root.subject, root_key, is_ca=True)
    leaf_key = _new_key()
    leaf = _sign(_LEAF_CN, leaf_key, intermediate.subject, int_key, san_dns=[_LEAF_CN])
    bundle = _pem(leaf) + _pem(intermediate)  # root deliberately omitted
    server = _TLSServer(tmp_path, bundle, _key_pem(leaf_key))
    try:
        result = await _dispatch_inspect(
            {"host": "127.0.0.1", "port": server.port, "server_name": _LEAF_CN}
        )
    finally:
        server.stop()
    assert result.status == "ok", result.error
    body = result.result
    assert len(body["chain"]) == 2
    assert body["chain"][-1]["self_signed"] is False
    assert body["chain_complete"] is False


# ---------------------------------------------------------------------------
# Probe allowlist (T1 foundation) + audit row records host:port
# ---------------------------------------------------------------------------


async def test_empty_allowlist_refuses_before_any_socket_opens(
    monkeypatch: pytest.MonkeyPatch,
    _registered_tls_inspect_op: None,
) -> None:
    """Empty allowlist ⇒ structured refusal, no socket opened."""

    def _boom(*_a: object, **_kw: object) -> object:
        raise AssertionError("create_connection must not run when the probe is refused")

    monkeypatch.setattr(net_tls.socket, "create_connection", _boom)
    result = await _dispatch_inspect({"host": "10.1.2.3", "port": 8443})
    assert result.status == "ok", result.error
    body = result.result
    assert body["handshake"] is False
    assert body["reason"] == "not_in_probe_allowlist"
    assert body["host"] == "10.1.2.3"
    assert body["port"] == 8443
    assert body["chain"] == []
    assert body["leaf"] is None


async def test_audit_row_records_literal_host_and_port(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    _registered_tls_inspect_op: None,
) -> None:
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    server = _self_signed_server(tmp_path)
    try:
        result = await _dispatch_inspect(
            {"host": "127.0.0.1", "port": server.port, "server_name": _LEAF_CN}
        )
    finally:
        server.stop()
    assert result.status == "ok", result.error

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        rows = list(
            (await session.execute(select(AuditLog).where(AuditLog.path == _OP_ID))).scalars().all()
        )
    assert len(rows) == 1
    raw = rows[0].raw_payload
    assert raw is not None
    assert raw["host"] == "127.0.0.1"
    assert raw["port"] == server.port
    assert raw["server_name"] == _LEAF_CN


# ---------------------------------------------------------------------------
# Return-failures contract — failed handshake is status=ok, never connector_*
# ---------------------------------------------------------------------------


async def test_refused_connect_is_ok_status_not_connector_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_tls_inspect_op: None,
) -> None:
    """A closed port returns handshake=false / reason=refused, status=ok."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # port is now (almost certainly) closed

    result = await _dispatch_inspect({"host": "127.0.0.1", "port": port})
    assert result.status == "ok", result.error
    assert result.extras.get("exception_class") is None
    assert result.result["handshake"] is False
    assert result.result["reason"] == "refused"
    assert result.result["chain"] == []


async def test_non_tls_endpoint_maps_to_tls_error(
    monkeypatch: pytest.MonkeyPatch,
    _registered_tls_inspect_op: None,
) -> None:
    """A plaintext endpoint (no TLS) is a normal failure, not an error."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "127.0.0.1")
    plain = socket.socket()
    plain.bind(("127.0.0.1", 0))
    plain.listen(1)
    port = plain.getsockname()[1]
    stop = threading.Event()

    def _serve() -> None:
        while not stop.is_set():
            plain.settimeout(0.5)
            try:
                client, _ = plain.accept()
            except (TimeoutError, OSError):
                continue
            # Speak plaintext at the TLS client, then close.
            with contextlib.suppress(OSError):
                client.sendall(b"HTTP/1.0 400 Bad Request\r\n\r\n")
                client.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        result = await _dispatch_inspect({"host": "127.0.0.1", "port": port, "timeout_seconds": 5})
    finally:
        stop.set()
        thread.join(timeout=2)
        plain.close()
    assert result.status == "ok", result.error
    assert result.result["handshake"] is False
    assert result.result["reason"] == "tls_error"


@pytest.mark.parametrize(
    "exc,expected_reason",
    [
        (socket.gaierror("name resolution failed"), "dns_failure"),
        (TimeoutError(), "timeout"),
        (ConnectionRefusedError(), "refused"),
        (OSError("network is unreachable"), "unreachable"),
    ],
    ids=["gaierror", "timeout", "refused", "other-oserror"],
)
async def test_handler_maps_connect_exceptions_to_reason_codes(
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
    expected_reason: str,
) -> None:
    """Every connect/handshake exception maps to a reason code, never re-raises."""
    monkeypatch.setenv(PROBE_ALLOWLIST_ENV, "203.0.113.5")

    def _raise(*_a: object, **_kw: object) -> object:
        raise exc

    monkeypatch.setattr(net_tls.socket, "create_connection", _raise)
    result = await net_tls_inspect(_make_operator(), None, {"host": "203.0.113.5", "port": 8443})
    assert result["handshake"] is False
    assert result["reason"] == expected_reason
    assert result["host"] == "203.0.113.5"
    assert result["port"] == 8443
    assert result["server_name"] == "203.0.113.5"


# ---------------------------------------------------------------------------
# Registration + classification
# ---------------------------------------------------------------------------


async def test_tls_inspect_registered_as_safe_ungated_typed_op(
    _registered_tls_inspect_op: None,
) -> None:
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = (
            await session.execute(
                select(EndpointDescriptor).where(
                    EndpointDescriptor.product == "net",
                    EndpointDescriptor.version == "1.x",
                    EndpointDescriptor.impl_id == "net-probe",
                    EndpointDescriptor.op_id == _OP_ID,
                )
            )
        ).scalar_one()
    assert row.source_kind == "typed"
    assert row.safety_level == "safe"
    assert row.requires_approval is False


def test_tls_inspect_classifies_as_read() -> None:
    from meho_backplane.broadcast.events import classify_op

    assert classify_op("net.tls_inspect") == "read"


# ---------------------------------------------------------------------------
# Pure-helper unit coverage
# ---------------------------------------------------------------------------


def test_encode_sni_omits_ip_literals() -> None:
    assert net_tls._encode_sni("10.0.0.5") is None
    assert net_tls._encode_sni("[2001:db8::1]") is None
    assert net_tls._encode_sni("appliance.local") == b"appliance.local"
    assert net_tls._encode_sni("host_underscore.internal") == b"host_underscore.internal"


def test_dns_name_wildcard_matching() -> None:
    assert net_tls._dns_name_matches(["*.example.com"], "a.example.com") is True
    assert net_tls._dns_name_matches(["*.example.com"], "example.com") is False
    assert net_tls._dns_name_matches(["*.example.com"], "a.b.example.com") is False
    assert net_tls._dns_name_matches(["Host.Example.com"], "host.example.com.") is True
