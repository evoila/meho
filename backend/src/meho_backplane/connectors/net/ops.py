# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed ops ``net.tcp_check`` / ``net.dns_lookup`` + registrar.

``net.*`` is a **synthetic** typed-op product like ``secret.*``: no
vendor connector backs it, so the package calls neither
``register_connector`` nor ``register_connector_v2``. The op is
registered under the natural key
``(product="net", version="1.x", impl_id="net-probe")``, so the wire
``connector_id`` is ``net-probe-1.x`` — which round-trips through
:func:`~meho_backplane.operations._lookup.parse_connector_id` back to
``("net", "1.x", "net-probe")`` (digit-led version, product is the
head's first hyphen segment). The handler is a **module-level**
function, so the dispatcher resolves it with ``connector_instance=None``
and ``target=None`` — the probe target is a param, not a registered
``Target``.

``net.tcp_check`` opens a TCP connection to ``host:port`` under a
bounded timeout, measures the connect latency, and closes immediately.
Three foundations every later ``net.*`` op reuses land here:

* **Probe allowlist** — the handler calls
  :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  on the exact dialed host *before* opening a socket.
  ``MEHO_NETDIAG_PROBE_ALLOWLIST`` empty ⇒ every probe refused (the
  connector is inert).
* **Audit-visible host:port** — the handler's return dict carries the
  literal ``host``/``port`` (unlike ``secret.move``'s refs, a
  host:port is not a secret), so the durable audit row's
  ``raw_payload`` answers "who probed what". The dispatcher stores the
  handler's return value as ``raw_payload`` verbatim.
* **Return-failures contract** — a refused, refused-by-peer, timed-out,
  or DNS-failed probe is the **product**, not an error: the handler
  returns ``{"connected": false, "reason": <code>, ...}`` with the
  dispatch ``status="ok"``. It never raises a ``connector_*`` error for
  a failed connection. Only an unexpected bug would propagate.

``safety_level="safe"`` + ``requires_approval=False`` make the probe
agent-auto-runnable and ungated, so the probe allowlist is the sole
floor. The reviewed alternative (``"caution"`` — operators auto-run,
agents do not) is a one-line change if the security review prefers it.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import time
from typing import TYPE_CHECKING, Any, Final

import dns.asyncresolver
import dns.exception
import dns.flags
import dns.rdatatype
import dns.resolver
import dns.reversename
import structlog

from meho_backplane.connectors.net.allowlist import (
    ProbeNotAllowedError,
    assert_probe_allowed,
)
from meho_backplane.operations.typed_register import register_typed_operation

if TYPE_CHECKING:
    from meho_backplane.auth.operator import Operator
    from meho_backplane.retrieval.embedding import EmbeddingService

__all__ = [
    "NET_DNS_LOOKUP_PARAMETER_SCHEMA",
    "NET_TCP_CHECK_PARAMETER_SCHEMA",
    "net_dns_lookup",
    "net_tcp_check",
    "register_net_typed_operations",
]

_log = structlog.get_logger(__name__)

#: Default connect timeout when the caller omits ``timeout_seconds``.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
#: Hard ceiling on the connect timeout — a probe must not become a way
#: to pin an event-loop task open indefinitely. Also the schema
#: ``maximum`` so the dispatcher rejects an over-long request before the
#: handler runs; the clamp is belt-and-suspenders for direct calls.
_MAX_TIMEOUT_SECONDS: Final[float] = 30.0

NET_TCP_CHECK_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "host": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Hostname or IP literal to probe. Must be covered by "
                "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused "
                "before any socket opens."
            ),
        },
        "port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": "TCP port to attempt a connection to.",
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Connect timeout in seconds (default 5, max 30). A "
                "connection that does not complete in time returns "
                "connected=false with reason='timeout'."
            ),
        },
    },
    "required": ["host", "port"],
    "additionalProperties": False,
}

_NET_TCP_CHECK_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "connected": {
            "type": "boolean",
            "description": "True iff the TCP handshake completed within the timeout.",
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: "
                "not_in_probe_allowlist, timeout, refused, dns_failure, unreachable."
            ),
        },
        "latency_ms": {
            "type": ["number", "null"],
            "description": "Connect latency in milliseconds on success; null on any failure.",
        },
        "host": {"type": "string", "description": "The probed host (audit-visible)."},
        "port": {"type": "integer", "description": "The probed port (audit-visible)."},
    },
    "required": ["connected", "reason", "latency_ms", "host", "port"],
    "additionalProperties": False,
}

_NET_TCP_CHECK_WHEN_TO_USE = (
    "Check whether a TCP port is reachable from the backplane's network "
    "vantage — e.g. 'can we reach the database on 5432?', 'is the load "
    "balancer's 443 open from here?'. A non-mutating reachability probe: "
    "it opens a connection, measures latency, and closes immediately. A "
    "failed connect (refused/timeout/DNS) is a normal result, not an "
    "error. The destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST."
)

_NET_TCP_CHECK_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to confirm TCP reachability of a host:port from the "
        "backplane before assuming a connectivity or firewall problem. "
        "Read-only: nothing is sent on the connection, it is closed "
        "right after the handshake."
    ),
    "parameter_hints": {
        "host": "Required. Hostname or IP literal. Must be allowlisted for probing.",
        "port": "Required. TCP port (1-65535).",
        "timeout_seconds": "Optional. Connect timeout (default 5, max 30).",
    },
    "output_shape": (
        "On success: {'connected': true, 'reason': null, 'latency_ms': "
        "<float>, 'host': <str>, 'port': <int>}. On a failed or refused "
        "probe: {'connected': false, 'reason': "
        "'<not_in_probe_allowlist|timeout|refused|dns_failure|unreachable>', "
        "'latency_ms': null, 'host': <str>, 'port': <int>} — still a "
        "successful (status=ok) op."
    ),
}


def _clamp_timeout(raw: Any) -> float:
    """Resolve ``timeout_seconds`` to a bounded float.

    The schema already bounds it for the dispatch path; this keeps a
    direct handler call (tests, other handlers) inside ``(0, MAX]`` too.
    """
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_SECONDS
    if value <= 0:
        return _DEFAULT_TIMEOUT_SECONDS
    return min(value, _MAX_TIMEOUT_SECONDS)


def _refusal(host: str, port: int, reason: str) -> dict[str, Any]:
    """Build the structured failure payload shared by every failed probe."""
    return {
        "connected": False,
        "reason": reason,
        "latency_ms": None,
        "host": host,
        "port": port,
    }


async def net_tcp_check(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Attempt a TCP connection to ``host:port`` and report reachability.

    Op-id: ``net.tcp_check``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). The dispatcher has validated the
    param schema, so ``host`` and ``port`` are present and well-typed.

    Flow: screen the host against the probe allowlist → open a
    connection under :func:`asyncio.wait_for` → measure latency → close.
    A refused/timed-out/DNS-failed connect returns a structured
    ``connected=false`` payload with ``status="ok"`` (the return-failures
    contract); the value is never raised as a ``connector_*`` error. The
    returned dict carries the literal ``host``/``port`` so the durable
    audit row's ``raw_payload`` records what was probed.
    """
    host = str(params["host"])
    port = int(params["port"])
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))

    try:
        assert_probe_allowed(host)
    except ProbeNotAllowedError:
        _log.info("net.tcp_check.refused", host=host, port=port, reason="not_in_probe_allowlist")
        return _refusal(host, port, "not_in_probe_allowlist")

    started = time.perf_counter()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except TimeoutError:
        # asyncio.wait_for raises builtin TimeoutError on timeout (3.11+).
        # Caught BEFORE OSError below since TimeoutError subclasses OSError.
        return _refusal(host, port, "timeout")
    except socket.gaierror:
        # DNS resolution failed (also an OSError subclass — catch first).
        return _refusal(host, port, "dns_failure")
    except ConnectionRefusedError:
        return _refusal(host, port, "refused")
    except OSError:
        # Network/host unreachable, no route, reset, etc.
        return _refusal(host, port, "unreachable")

    latency_ms = (time.perf_counter() - started) * 1000.0
    # Close the probe connection promptly; a failure to close cleanly
    # does not change the reachability answer we already have.
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()

    return {
        "connected": True,
        "reason": None,
        "latency_ms": latency_ms,
        "host": host,
        "port": port,
    }


#: Record types ``net.dns_lookup`` accepts for a forward query. Bounded
#: on purpose: metaqueries (``ANY``) and zone transfers (``AXFR``) are
#: out of scope (#2409), so restricting the enum keeps them unreachable
#: at the dispatcher's param-schema gate rather than in the handler.
_DNS_RECORD_TYPES: Final[tuple[str, ...]] = (
    "A",
    "AAAA",
    "CNAME",
    "MX",
    "TXT",
    "SRV",
    "NS",
    "SOA",
    "PTR",
)

NET_DNS_LOOKUP_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Name to look up: a hostname for a forward query, or an "
                "IP literal for a reverse (PTR) lookup. Must be covered "
                "by MEHO_NETDIAG_PROBE_ALLOWLIST (a hostname verbatim, an "
                "IP by range) or the lookup is refused before any query."
            ),
        },
        "type": {
            "type": "string",
            "enum": list(_DNS_RECORD_TYPES),
            "description": (
                "DNS record type for a forward query (default A). Ignored "
                "when name is an IP literal — a reverse PTR lookup runs."
            ),
        },
        "resolver": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional resolver IP to query instead of the system "
                "resolver — compare 'what the pod's resolver returns' vs "
                "an authoritative/other nameserver (split-horizon). Must "
                "be an IP literal and itself be allowlisted."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Overall query deadline in seconds (default 5, max 30). "
                "A query that does not complete in time returns "
                "resolved=false with reason='timeout'."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}

_NET_DNS_LOOKUP_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "resolved": {
            "type": "boolean",
            "description": "True iff the query returned at least one record.",
        },
        "name": {"type": "string", "description": "The queried name (audit-visible)."},
        "type": {
            "type": "string",
            "description": "The record type actually queried (PTR for a reverse lookup).",
        },
        "resolver": {
            "type": "string",
            "description": "'system' or the chosen resolver IP (audit-visible).",
        },
        "records": {
            "type": "array",
            "description": "Answer records; empty on any failure.",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "value": {"type": "string"},
                    "ttl": {"type": "integer"},
                },
                "required": ["type", "value", "ttl"],
                "additionalProperties": False,
            },
        },
        "authoritative": {
            "type": ["boolean", "null"],
            "description": "AA flag on the answer; null on any failure.",
        },
        "authenticated_data": {
            "type": ["boolean", "null"],
            "description": (
                "AD (DNSSEC) flag as reported by the resolver — reported, "
                "not validated (chain validation is out of scope). Null on failure."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on success; otherwise a failure code: "
                "not_in_probe_allowlist, bad_resolver, nxdomain, no_answer, "
                "servfail, timeout, no_resolver."
            ),
        },
    },
    "required": [
        "resolved",
        "name",
        "type",
        "resolver",
        "records",
        "authoritative",
        "authenticated_data",
        "reason",
    ],
    "additionalProperties": False,
}

_NET_DNS_LOOKUP_WHEN_TO_USE = (
    "Resolve DNS from the backplane's vantage — 'what A record does this "
    "host have here?', 'what MX/TXT/SRV/NS/SOA records exist?', reverse "
    "PTR for an IP, or compare the pod resolver's answer against an "
    "authoritative/other nameserver (the split-horizon case). A read-only "
    "lookup; NXDOMAIN / no-answer / SERVFAIL / timeout are normal results "
    "(resolved=false with a reason), not errors. The queried name and any "
    "chosen resolver must be inside MEHO_NETDIAG_PROBE_ALLOWLIST."
)

_NET_DNS_LOOKUP_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to resolve a name (or reverse-resolve an IP) from the "
        "backplane, optionally against a chosen resolver, before assuming "
        "a name-resolution or split-horizon problem. Read-only."
    ),
    "parameter_hints": {
        "name": "Required. Hostname (forward) or IP literal (reverse PTR). Must be allowlisted.",
        "type": "Optional. Record type for a forward query (default A). One of "
        + ", ".join(_DNS_RECORD_TYPES)
        + ". Ignored for an IP (reverse PTR).",
        "resolver": "Optional. Resolver IP to query instead of the system resolver; "
        "must be an allowlisted IP literal.",
        "timeout_seconds": "Optional. Query deadline (default 5, max 30).",
    },
    "output_shape": (
        "On success: {'resolved': true, 'name': <str>, 'type': <str>, "
        "'resolver': 'system'|<ip>, 'records': [{'type', 'value', 'ttl'}], "
        "'authoritative': <bool>, 'authenticated_data': <bool>, 'reason': "
        "null}. On failure: the same shape with 'resolved': false, empty "
        "'records', null flags, and 'reason': "
        "'<not_in_probe_allowlist|bad_resolver|nxdomain|no_answer|servfail|"
        "timeout|no_resolver>' — still a successful (status=ok) op."
    ),
}


def _dns_refusal(name: str, record_type: str, resolver: str, reason: str) -> dict[str, Any]:
    """Build the structured failure payload shared by every failed lookup."""
    return {
        "resolved": False,
        "name": name,
        "type": record_type,
        "resolver": resolver,
        "records": [],
        "authoritative": None,
        "authenticated_data": None,
        "reason": reason,
    }


def _as_ip_literal(value: str) -> str | None:
    """Return *value* as a bare IP string if it is an IP literal, else None.

    Accepts the bracketed IPv6 URL form (``[::1]``) like the allowlist so
    a caller can pass either shape. Used both to detect a reverse-lookup
    request (name is an IP) and to validate a chosen resolver.
    """
    candidate = value.strip()
    literal = (
        candidate[1:-1] if candidate.startswith("[") and candidate.endswith("]") else candidate
    )
    try:
        ipaddress.ip_address(literal)
    except ValueError:
        return None
    return literal


def _build_resolver(resolver_ip: str | None) -> dns.asyncresolver.Resolver | None:
    """Construct the async resolver, or None if the system resolver is unset.

    ``configure=True`` reads ``/etc/resolv.conf`` for the system resolver;
    a chosen resolver needs none (its IP is pinned as the sole
    nameserver). Returns None when there is no ``/etc/resolv.conf`` *and*
    no chosen resolver — the caller maps that to the ``no_resolver``
    reason rather than surfacing a bug.
    """
    try:
        resolver = dns.asyncresolver.Resolver(configure=resolver_ip is None)
    except dns.resolver.NoResolverConfiguration:
        return None
    if resolver_ip is not None:
        resolver.nameservers = [resolver_ip]
    return resolver


async def _run_dns_query(
    resolver: dns.asyncresolver.Resolver,
    qname: Any,
    query_type: str,
    timeout: float,
) -> tuple[list[dict[str, Any]], int] | str:
    """Resolve *qname*; return ``(records, flags)`` or a failure reason code.

    A lookup failure (NXDOMAIN / no-answer / SERVFAIL / timeout) is
    mapped to its reason string instead of raised — the return-failures
    contract. ``LifetimeTimeout`` subclasses ``dns.exception.Timeout``, so
    the deadline case is caught by that arm.
    """
    try:
        answer = await resolver.resolve(
            qname,
            query_type,
            lifetime=timeout,
            search=False,
            raise_on_no_answer=True,
        )
    except dns.resolver.NXDOMAIN:
        return "nxdomain"
    except dns.resolver.NoAnswer:
        return "no_answer"
    except dns.resolver.NoNameservers:
        return "servfail"
    except dns.exception.Timeout:
        return "timeout"
    rrset = answer.rrset
    if rrset is None:
        # raise_on_no_answer=True normally makes this unreachable; treat a
        # missing rrset as no_answer rather than dereferencing None.
        return "no_answer"
    records = [
        {
            "type": dns.rdatatype.to_text(rdata.rdtype),
            "value": rdata.to_text(),
            "ttl": int(rrset.ttl),
        }
        for rdata in rrset
    ]
    return records, int(answer.response.flags)


async def net_dns_lookup(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve DNS records (forward or reverse) from the backplane vantage.

    Op-id: ``net.dns_lookup``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). Uses ``dns.asyncresolver`` so the
    query runs off the event loop natively.

    Flow: reject a non-IP ``resolver`` → screen ``name`` (and any custom
    resolver IP) through the probe allowlist → build a forward query for
    the requested ``type`` or a reverse PTR query when ``name`` is an IP
    literal → resolve under the timeout as the query deadline. NXDOMAIN /
    no-answer / SERVFAIL / timeout return a structured ``resolved=false``
    payload with ``status="ok"`` (the return-failures contract); none are
    raised as ``connector_*`` errors. The returned dict carries the
    literal ``name``/``type``/``resolver`` so the durable audit row's
    ``raw_payload`` records what was looked up and against which server.
    """
    name = str(params["name"]).strip()
    requested_type = str(params.get("type") or "A").upper()
    raw_resolver = params.get("resolver")
    resolver_ip = str(raw_resolver).strip() if raw_resolver not in (None, "") else None
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    resolver_label = resolver_ip or "system"

    # A chosen resolver must be an IP literal: dnspython's ``nameservers``
    # setter rejects a hostname, and an unresolved name pins no server.
    if resolver_ip is not None and _as_ip_literal(resolver_ip) is None:
        return _dns_refusal(name, requested_type, resolver_label, "bad_resolver")

    # One guard applied uniformly (#1177): the queried name is gated, and
    # a custom resolver IP is gated too — querying an internal resolver or
    # resolving internal names is itself mild recon.
    try:
        assert_probe_allowed(name)
        if resolver_ip is not None:
            assert_probe_allowed(resolver_ip)
    except ProbeNotAllowedError:
        _log.info("net.dns_lookup.refused", name=name, reason="not_in_probe_allowlist")
        return _dns_refusal(name, requested_type, resolver_label, "not_in_probe_allowlist")

    # Reverse (PTR) form when the name is an IP literal — mirrors ``dig -x``.
    ip_literal = _as_ip_literal(name)
    if ip_literal is not None:
        qname: Any = dns.reversename.from_address(ip_literal)
        query_type = "PTR"
    else:
        qname = name
        query_type = requested_type

    resolver = _build_resolver(resolver_ip)
    if resolver is None:
        # No /etc/resolv.conf and no chosen resolver — nothing to ask.
        return _dns_refusal(name, query_type, resolver_label, "no_resolver")

    outcome = await _run_dns_query(resolver, qname, query_type, timeout)
    if isinstance(outcome, str):
        return _dns_refusal(name, query_type, resolver_label, outcome)

    records, flags = outcome
    return {
        "resolved": True,
        "name": name,
        "type": query_type,
        "resolver": resolver_label,
        "records": records,
        "authoritative": bool(flags & dns.flags.AA),
        # Reported, not validated — DNSSEC chain validation is out of scope.
        "authenticated_data": bool(flags & dns.flags.AD),
        "reason": None,
    }


async def register_net_typed_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.*`` typed ops into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. Every ``net.*``
    op is ``safe`` + ``requires_approval=False`` — the probe allowlist is
    the only floor. Registers ``net.tcp_check`` (#2406) and
    ``net.dns_lookup`` (#2409).
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.tcp_check",
        handler=net_tcp_check,
        group_key="probe",
        when_to_use=_NET_TCP_CHECK_WHEN_TO_USE,
        summary="Check whether a TCP host:port is reachable from the backplane.",
        description=(
            "Opens a TCP connection to a host:port under a bounded "
            "timeout, measures the connect latency, and closes "
            "immediately — a non-mutating reachability probe. The "
            "destination must be inside MEHO_NETDIAG_PROBE_ALLOWLIST or "
            "the probe is refused before any socket opens (empty "
            "allowlist ⇒ the connector is inert). A refused, timed-out, "
            "or DNS-failed connect returns connected=false with a reason "
            "code and status=ok — a failed probe is the product, never a "
            "connector error."
        ),
        parameter_schema=NET_TCP_CHECK_PARAMETER_SCHEMA,
        response_schema=_NET_TCP_CHECK_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_TCP_CHECK_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.dns_lookup",
        handler=net_dns_lookup,
        group_key="probe",
        when_to_use=_NET_DNS_LOOKUP_WHEN_TO_USE,
        summary="Resolve DNS records (forward or reverse) from the backplane, dig-parity.",
        description=(
            "Resolves DNS from the backplane's vantage via dnspython: "
            "forward records of a chosen type (A/AAAA/CNAME/MX/TXT/SRV/"
            "NS/SOA) or a reverse PTR lookup when the name is an IP "
            "literal, optionally against a chosen resolver IP so an "
            "operator can compare the pod resolver against an "
            "authoritative/other nameserver (split-horizon). The queried "
            "name and any chosen resolver must be inside "
            "MEHO_NETDIAG_PROBE_ALLOWLIST. NXDOMAIN, no-answer, SERVFAIL "
            "and timeout return resolved=false with a reason code and "
            "status=ok — a failed lookup is the product, never a "
            "connector error."
        ),
        parameter_schema=NET_DNS_LOOKUP_PARAMETER_SCHEMA,
        response_schema=_NET_DNS_LOOKUP_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "dns"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_DNS_LOOKUP_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
