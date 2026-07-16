# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.tls_inspect`` + its registrar.

The second ``net.*`` op (#2407) on the T1 keystone (#2406). It performs
an ``openssl s_client -showcerts``-parity read: open a TLS handshake to
``host:port`` with certificate verification **off** (self-signed
appliances are the point) and report the **full presented certificate
chain** the server sent, leaf-first, plus chain-level completeness, the
leaf hostname match, negotiated protocol, and cipher.

Why pyOpenSSL and not stdlib ``ssl``: on the ``requires-python`` floor
(3.12) ``ssl.SSLSocket.getpeercert`` returns only the **leaf** cert; the
full-chain ``ssl.SSLSocket.get_unverified_chain`` is 3.13+. pyOpenSSL's
:meth:`OpenSSL.SSL.Connection.get_peer_cert_chain` returns the whole
chain the peer presented on an unverified handshake, and its
``as_cryptography=True`` mode (pyOpenSSL 24.3+) hands back
:class:`cryptography.x509.Certificate` objects directly, so the parse
reuses the ``cryptography`` x509 API already used elsewhere in the tree.

This op inherits the three T1 foundations verbatim:

* **Probe allowlist** — :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  screens the dialed ``host`` *before* any socket opens
  (``MEHO_NETDIAG_PROBE_ALLOWLIST`` empty ⇒ every probe refused).
* **Audit-visible host:port** — the return dict carries the literal
  ``host``/``port``/``server_name`` (a host:port is not a secret), so the
  durable audit row's ``raw_payload`` answers "who inspected what".
* **Return-failures contract** — a refused, timed-out, DNS-failed, or
  non-TLS endpoint is the **product**, not an error: the handler returns
  ``{"handshake": false, "reason": <code>, ...}`` with dispatch
  ``status="ok"``. A self-signed / expired / hostname-mismatched cert is
  **inspected and reported** (``handshake=true``), never rejected —
  verification is off by design, so the cert is data, not a failure.

``safety_level="safe"`` + ``requires_approval=False``: a read-only
handshake that sends no application bytes, so the probe allowlist is the
sole floor (same posture as ``net.tcp_check``).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import select
import socket
import time
from typing import TYPE_CHECKING, Any

import structlog
from cryptography import x509
from cryptography.x509.oid import NameOID
from OpenSSL import SSL

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)

# Reuse the shared probe timeout bounds/clamp from the keystone module.
# ``ops`` never imports ``tls`` (the __init__ queues each registrar
# independently), so this package-internal import forms no cycle.
from meho_backplane.connectors.net.ops import (
    _DEFAULT_TIMEOUT_SECONDS,
    _MAX_TIMEOUT_SECONDS,
    _clamp_timeout,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_TLS_INSPECT_PARAMETER_SCHEMA",
    "net_tls_inspect",
    "register_net_tls_inspect_operation",
]

_log = structlog.get_logger(__name__)

NET_TLS_INSPECT_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP literal to open the TLS handshake to. Must "
                "be covered by MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is "
                "refused before any socket opens."
            ),
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "TCP port the TLS service listens on (e.g. 443, 8443, 636).",
        },
        "server_name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "SNI server name to send and to match the leaf certificate "
                "against. Defaults to host. Set it when the endpoint is "
                "dialed by IP but presents a name-based virtual host."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Combined connect + handshake timeout in seconds (default "
                "5, max 30). A handshake that does not complete in time "
                "returns handshake=false with reason='timeout'."
            ),
        },
    },
    "required": ["host", "port"],
    "additionalProperties": False,
}

_CHAIN_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subject": {"type": "string", "description": "RFC 4514 subject DN."},
        "issuer": {"type": "string", "description": "RFC 4514 issuer DN."},
        "san": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Subject Alternative Names (DNS names then IP literals).",
        },
        "not_before": {"type": "string", "description": "ISO 8601 UTC notBefore."},
        "not_after": {"type": "string", "description": "ISO 8601 UTC notAfter."},
        "serial": {"type": "string", "description": "Serial number as a decimal string."},
        "self_signed": {
            "type": "boolean",
            "description": "True iff subject == issuer (a root or self-signed leaf).",
        },
    },
    "required": ["subject", "issuer", "san", "not_before", "not_after", "serial", "self_signed"],
    "additionalProperties": False,
}

_NET_TLS_INSPECT_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "handshake": {
            "type": "boolean",
            "description": "True iff the TLS handshake completed (verification is off).",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on a completed handshake; otherwise a failure code: "
                "not_in_probe_allowlist, timeout, refused, dns_failure, "
                "unreachable, tls_error."
            ),
        },
        "host": {"type": "string", "description": "The dialed host (audit-visible)."},
        "port": {"type": "integer", "description": "The dialed port (audit-visible)."},
        "server_name": {
            "type": "string",
            "description": "The SNI / hostname-match name used (audit-visible).",
        },
        "protocol": {
            "type": ["string", "null"],
            "description": "Negotiated TLS protocol (e.g. 'TLSv1.3'); null on failure.",
        },
        "cipher": {
            "type": ["string", "null"],
            "description": "Negotiated cipher suite; null on failure.",
        },
        "chain": {
            "type": "array",
            "items": _CHAIN_ITEM_SCHEMA,
            "description": "Presented certificate chain, leaf-first (empty on failure).",
        },
        "leaf": {
            **{k: v for k, v in _CHAIN_ITEM_SCHEMA.items() if k != "type"},
            "type": ["object", "null"],
            "description": "Convenience alias for chain[0]; null on failure.",
        },
        "not_after": {
            "type": ["string", "null"],
            "description": "Leaf notAfter (ISO 8601 UTC) for the common 'is it expiring' read.",
        },
        "hostname_match": {
            "type": "boolean",
            "description": (
                "True iff server_name matches the leaf SAN (or CN when no SAN), "
                "computed independently of the disabled stack verification."
            ),
        },
        "chain_complete": {
            "type": "boolean",
            "description": "True iff the last presented cert is self-signed (a root was sent).",
        },
    },
    "required": [
        "handshake",
        "reason",
        "host",
        "port",
        "server_name",
        "protocol",
        "cipher",
        "chain",
        "leaf",
        "not_after",
        "hostname_match",
        "chain_complete",
    ],
    "additionalProperties": False,
}

_NET_TLS_INSPECT_WHEN_TO_USE = (
    "Inspect the full TLS certificate chain an endpoint presents — the "
    "openssl 's_client -showcerts' read: 'what cert does the load "
    "balancer serve on 443?', 'is this appliance's cert expired or "
    "self-signed?', 'did the server send the intermediate?'. Verification "
    "is OFF, so a self-signed / expired / mismatched cert is inspected and "
    "reported, never rejected — the cert is the answer. A refused, "
    "timed-out, or non-TLS endpoint is a normal result, not an error. The "
    "destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST."
)

_NET_TLS_INSPECT_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to read the certificate chain a TLS endpoint presents "
        "(leaf, intermediates, root-if-sent) without trusting it: chain "
        "completeness, per-cert validity window, self-signed flags, and "
        "whether the leaf matches a hostname. Read-only: no application "
        "bytes are sent."
    ),
    "parameter_hints": {
        "host": "Required. Hostname or IP literal. Must be allowlisted for probing.",
        "port": "Required. TLS port (e.g. 443, 8443, 636).",
        "server_name": "Optional. SNI + hostname-match name; defaults to host.",
        "timeout_seconds": "Optional. Connect+handshake timeout (default 5, max 30).",
    },
    "output_shape": (
        "On a completed handshake: {'handshake': true, 'reason': null, "
        "'chain': [{subject, issuer, san, not_before, not_after, serial, "
        "self_signed}, ...] (leaf-first), 'leaf': <chain[0]>, "
        "'hostname_match': <bool>, 'chain_complete': <bool>, 'protocol': "
        "<str>, 'cipher': <str>, 'not_after': <leaf notAfter>, 'host', "
        "'port', 'server_name'}. On a refused / timed-out / non-TLS "
        "endpoint: the same keys with handshake=false, reason set, chain=[] "
        "and leaf=null — still a successful (status=ok) op."
    ),
}


def _encode_sni(server_name: str) -> bytes | None:
    """Return the SNI bytes for *server_name*, or ``None`` to omit SNI.

    SNI carries a DNS hostname, never an IP literal (RFC 6066 §3), so an
    IP ``server_name`` yields ``None``. A hostname is encoded to its IDNA
    A-label form, falling back to ASCII (underscore-bearing internal names
    are not valid IDNA but are valid ASCII); an un-encodable value omits
    SNI rather than failing the handshake.
    """
    candidate = server_name.strip().rstrip(".")
    if not candidate:
        return None
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        ipaddress.ip_address(literal)
        return None
    except ValueError:
        pass
    try:
        return candidate.encode("idna")
    except (UnicodeError, ValueError):
        try:
            return candidate.encode("ascii")
        except UnicodeError:
            return None


def _san(cert: x509.Certificate) -> x509.SubjectAlternativeName | None:
    """Return the leaf's SubjectAlternativeName extension value, or ``None``."""
    try:
        return cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound:
        return None


def _san_dns_names(cert: x509.Certificate) -> list[str]:
    """Return the leaf's SAN dNSName values, or ``[]`` when absent."""
    san = _san(cert)
    return list(san.get_values_for_type(x509.DNSName)) if san is not None else []


def _san_ip_addresses(cert: x509.Certificate) -> list[str]:
    """Return the leaf's SAN iPAddress values as strings, or ``[]``."""
    san = _san(cert)
    return [str(ip) for ip in san.get_values_for_type(x509.IPAddress)] if san is not None else []


def _common_name(cert: x509.Certificate) -> str | None:
    """Return the subject Common Name, or ``None`` when the cert has none."""
    attrs = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(attrs[0].value) if attrs else None


def _dns_name_matches(patterns: list[str], hostname: str) -> bool:
    """Match *hostname* against DNS *patterns*, honouring a leftmost wildcard.

    A ``*.example.com`` pattern matches exactly one extra leftmost label
    (``a.example.com`` but not ``example.com`` or ``a.b.example.com``) —
    the RFC 6125 wildcard rule. Matching is case-insensitive with trailing
    dots stripped.
    """
    host = hostname.rstrip(".").lower()
    for raw in patterns:
        pattern = raw.rstrip(".").lower()
        if not pattern:
            continue
        if pattern == host:
            return True
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            if host.endswith(suffix):
                leftmost = host[: -len(suffix)]
                if leftmost and "." not in leftmost:
                    return True
    return False


def _leaf_hostname_match(cert: x509.Certificate, server_name: str) -> bool:
    """Return whether *server_name* matches the leaf cert.

    Computed **independently** of the TLS stack because the inspection
    context disables ``check_hostname`` (verification is off). An IP
    ``server_name`` is matched against the SAN iPAddress entries; a
    hostname against the SAN dNSName entries (wildcard-aware), falling
    back to the subject CN only when the cert carries no SAN dNSName
    (legacy appliances).
    """
    name = server_name.strip().rstrip(".")
    literal = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        target_ip = ipaddress.ip_address(literal)
    except ValueError:
        target_ip = None
    if target_ip is not None:
        return any(target_ip == ipaddress.ip_address(entry) for entry in _san_ip_addresses(cert))
    dns_names = _san_dns_names(cert)
    if dns_names:
        return _dns_name_matches(dns_names, name)
    common_name = _common_name(cert)
    return common_name is not None and _dns_name_matches([common_name], name)


def _cert_to_dict(cert: x509.Certificate) -> dict[str, Any]:
    """Flatten one presented certificate into the audit-safe report shape.

    ``serial`` is stringified (a serial is a large integer that a JS
    consumer would silently truncate as a JSON number); timestamps use the
    tz-aware ``*_utc`` accessors (naive ``not_valid_after`` is deprecated
    in ``cryptography``).
    """
    return {
        "subject": cert.subject.rfc4514_string(),
        "issuer": cert.issuer.rfc4514_string(),
        "san": _san_dns_names(cert) + _san_ip_addresses(cert),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": cert.not_valid_after_utc.isoformat(),
        "serial": str(cert.serial_number),
        "self_signed": cert.subject == cert.issuer,
    }


def _failure(host: str, port: int, server_name: str, reason: str) -> dict[str, Any]:
    """Build the uniform structured payload shared by every failed inspection."""
    return {
        "handshake": False,
        "reason": reason,
        "host": host,
        "port": port,
        "server_name": server_name,
        "protocol": None,
        "cipher": None,
        "chain": [],
        "leaf": None,
        "not_after": None,
        "hostname_match": False,
        "chain_complete": False,
    }


def _run_until_ready(sock: socket.socket, deadline: float, action: Any) -> Any:
    """Drive a pyOpenSSL non-blocking op to completion under a deadline.

    A socket with a timeout is non-blocking under the hood, so pyOpenSSL
    raises :class:`~OpenSSL.SSL.WantReadError` /
    :class:`~OpenSSL.SSL.WantWriteError` instead of blocking. Re-run
    *action* after ``select``-waiting for readiness, bounded by
    ``deadline`` (raising :class:`TimeoutError` when it lapses) so a stalled
    peer can never pin the worker thread past the caller's timeout.
    """
    while True:
        try:
            return action()
        except SSL.WantReadError:
            read_fds, write_fds = [sock], []
        except SSL.WantWriteError:
            read_fds, write_fds = [], [sock]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("tls handshake timed out")
        select.select(read_fds, write_fds, [], remaining)


def _blocking_tls_inspect(
    host: str, port: int, server_name: str, timeout: float
) -> tuple[list[x509.Certificate], str, str]:
    """Open an unverified TLS handshake and return (chain, protocol, cipher).

    Runs off the event loop (the caller wraps it in
    :func:`asyncio.to_thread`). The context disables verification
    (``VERIFY_NONE``) so a self-signed / expired / mismatched cert
    completes the handshake and is returned for inspection rather than
    rejected. The full presented chain is pulled via
    ``get_peer_cert_chain(as_cryptography=True)`` (pyOpenSSL 24.3+), which
    returns :class:`cryptography.x509.Certificate` objects directly.
    Connect and handshake share one ``deadline`` derived from *timeout*.
    """
    deadline = time.monotonic() + timeout
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        context = SSL.Context(SSL.TLS_CLIENT_METHOD)  # NOSONAR(S4423) — probe negotiates whatever the appliance offers; protocol version is reported output  # noqa: E501  # fmt: skip
        context.set_verify(SSL.VERIFY_NONE)  # NOSONAR(S4830) — inspection-only handshake; verification-off is the op's purpose (module docstring)  # noqa: E501  # fmt: skip
        connection = SSL.Connection(context, raw)  # NOSONAR(S5527) — hostname match is computed and REPORTED by the handler, not enforced  # noqa: E501  # fmt: skip
        connection.set_connect_state()
        sni = _encode_sni(server_name)
        if sni is not None:
            connection.set_tlsext_host_name(sni)
        _run_until_ready(raw, deadline, connection.do_handshake)
        chain = connection.get_peer_cert_chain(as_cryptography=True) or []
        protocol = connection.get_protocol_version_name()
        cipher = connection.get_cipher_name() or ""
        # A clean bidirectional close-notify is best-effort; the chain we
        # already read is the product, so a shutdown hiccup is moot.
        with contextlib.suppress(SSL.Error, OSError, TimeoutError):
            _run_until_ready(raw, deadline, connection.shutdown)
        return list(chain), protocol, cipher
    finally:
        raw.close()


def _connect_failure_reason(exc: BaseException) -> str:
    """Map a connect/handshake exception to a return-failures reason code.

    Order is significant: :class:`socket.gaierror`, :class:`TimeoutError`
    (which ``socket.timeout`` aliases and ``_run_until_ready``'s
    deadline-lapse raises), and :class:`ConnectionRefusedError` are all
    :class:`OSError` subclasses, so they are checked before the generic
    ``OSError`` fallthrough. :class:`~OpenSSL.SSL.Error` (peer is not TLS,
    protocol/alert, reset mid-handshake) is not an ``OSError``.
    """
    if isinstance(exc, socket.gaierror):
        return "dns_failure"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "refused"
    if isinstance(exc, SSL.Error):
        return "tls_error"
    return "unreachable"


async def net_tls_inspect(
    operator: Operator, target: Any, params: dict[str, Any]
) -> dict[str, Any]:
    """Inspect the full presented TLS certificate chain of ``host:port``.

    Op-id: ``net.tls_inspect``. Synthetic typed op (``target`` is always
    ``None``); the dispatcher has validated the schema. Flow: screen
    ``host`` against the probe allowlist → run an unverified pyOpenSSL
    handshake off the event loop → flatten each presented cert via the
    ``cryptography`` x509 API → compute ``hostname_match`` /
    ``chain_complete`` locally (the stack did not verify). A refused,
    timed-out, DNS-failed, or non-TLS endpoint returns ``handshake=false``
    with ``status="ok"`` (the return-failures contract); a self-signed /
    expired / mismatched cert is **inspected**, never failed. The return
    dict carries the literal ``host``/``port``/``server_name`` for the
    durable audit row.
    """
    host = str(params["host"])
    port = int(params["port"])
    raw_server_name = str(params.get("server_name") or "").strip()
    server_name = raw_server_name or host
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.tls_inspect.refused", host=host, port=port, reason="not_in_probe_allowlist")
        return _failure(host, port, server_name, "not_in_probe_allowlist")

    try:
        chain, protocol, cipher = await asyncio.to_thread(
            _blocking_tls_inspect, host, port, server_name, timeout
        )
    except (OSError, SSL.Error) as exc:
        # Every connect/handshake failure is the product, not an error:
        # map it to a reason code and return status="ok". SSL.Error is not
        # an OSError, so both bases are caught here.
        return _failure(host, port, server_name, _connect_failure_reason(exc))

    chain_dicts = [_cert_to_dict(cert) for cert in chain]
    leaf = chain_dicts[0] if chain_dicts else None
    hostname_match = bool(chain) and _leaf_hostname_match(chain[0], server_name)
    chain_complete = bool(chain) and (chain[-1].subject == chain[-1].issuer)

    return {
        "handshake": True,
        "reason": None,
        "host": host,
        "port": port,
        "server_name": server_name,
        "protocol": protocol,
        "cipher": cipher,
        "chain": chain_dicts,
        "leaf": leaf,
        "not_after": leaf["not_after"] if leaf else None,
        "hostname_match": hostname_match,
        "chain_complete": chain_complete,
    }


async def register_net_tls_inspect_operation(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.tls_inspect`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (a sibling registrar to ``net.tcp_check``'s), run after
    the connector eager-import pass. Registered under the same synthetic
    natural key as the keystone
    (``product="net", version="1.x", impl_id="net-probe"``), so both ops
    share the ``net-probe-1.x`` wire ``connector_id``. Idempotent. ``safe``
    + ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.tls_inspect",
        handler=net_tls_inspect,
        group_key="probe",
        when_to_use=_NET_TLS_INSPECT_WHEN_TO_USE,
        summary="Inspect the full presented TLS certificate chain of a host:port.",
        description=(
            "Opens a TLS handshake to a host:port with certificate "
            "verification OFF and reports the full chain the server "
            "presents (leaf → intermediates → root-if-sent), leaf-first: "
            "per-cert subject / SAN / issuer / validity window / serial / "
            "self-signed flag, plus chain completeness, the leaf hostname "
            "match (computed independently of the disabled verification), "
            "and the negotiated protocol and cipher — openssl 's_client "
            "-showcerts' parity. A self-signed / expired / mismatched cert "
            "is inspected and reported, never rejected. The destination "
            "must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is "
            "refused before any socket opens. A refused, timed-out, or "
            "non-TLS endpoint returns handshake=false with a reason code "
            "and status=ok — a failed handshake is the product, never a "
            "connector error."
        ),
        parameter_schema=NET_TLS_INSPECT_PARAMETER_SCHEMA,
        response_schema=_NET_TLS_INSPECT_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "tls"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_TLS_INSPECT_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
