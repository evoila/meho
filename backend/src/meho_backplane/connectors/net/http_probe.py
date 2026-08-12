# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Network-diagnostics typed op ``net.http_probe`` + its registrar.

``net.http_probe`` is T3 of the ``net.*`` family (Initiative #2405). It
issues a **single** HTTP request from the backplane's network vantage to
an operator-named URL and reports the reachability/identity surface —
status, response headers, the redirect chain, a TLS summary, and
timing — but **never the response body**. Only the body's ``body_size``
and ``body_sha256`` are returned, so the op is a reachability/identity
probe, not a fetch/exfil path (the deliberate anti-exfil floor).

It reuses the three foundations the T1 keystone (``net.tcp_check``,
#2406) established, in the ``secret.*`` synthetic-connector mold:

* **Probe allowlist** — the handler calls
  :func:`~meho_backplane.connectors.net.allowlist.assert_probe_allowed`
  on the exact host it is about to dial, *before* any socket opens, and
  **again on every redirect hop's host before following it**. An HTTP
  redirect that bounces to a non-allowlisted host (open-redirect SSRF —
  the concern already noted at ``adapters/http.py:260``) halts the walk
  with a structured ``{"blocked_redirect": "<host>"}`` result; the
  redirect target is never dialed. ``MEHO_NETDIAG_PROBE_ALLOWLIST``
  empty ⇒ every probe refused (the connector is inert). The two
  refusals are deliberately asymmetric (#2784): the **initial** host
  being refused means nothing was dialed at all, so it propagates and
  the dispatcher renders it as ``connector_probe_refused``; a refused
  **redirect** hop keeps ``status="ok"`` with ``reachable=true``,
  because the prior hop did answer — that is a real observation.
* **Audit-visible URL** — the handler's return dict carries the literal
  requested ``url`` and the ``final_url`` actually reached (a URL is not
  a secret), so the durable audit row's ``raw_payload`` answers "who
  probed what". The dispatcher stores the handler's return value as
  ``raw_payload`` verbatim.
* **Return-failures contract** — a refused, timed-out, DNS-failed,
  TLS-failed, redirect-blocked, or too-many-redirects probe is the
  **product**, not an error: the handler returns
  ``{"reachable": false | true, "reason": <code>, ...}`` with the
  dispatch ``status="ok"``. It never raises a ``connector_*`` error for
  a network-level outcome. Only an unexpected bug would propagate.

Unlike the target-coupled :class:`~meho_backplane.connectors.adapters.http.HttpConnector`
(per-target pooled client, keyed on ``target_cache_key``), this op uses
a **fresh** ``httpx.AsyncClient(follow_redirects=False)`` per call and
walks redirects manually so it can re-gate each hop — httpx's own
redirect follower would dial the next host before the allowlist could
see it. ``safety_level="safe"`` + ``requires_approval=False`` make the
probe agent-auto-runnable; the probe allowlist is the sole floor.
"""

from __future__ import annotations

import asyncio
import hashlib
import socket
import ssl
import time
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Final

import anyio
import httpx
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
    "NET_HTTP_PROBE_PARAMETER_SCHEMA",
    "net_http_probe",
    "register_net_http_probe_operations",
]

_log = structlog.get_logger(__name__)

#: Default per-request + total timeout when the caller omits it.
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 5.0
#: Hard ceiling on the timeout — a probe must not pin an event-loop task
#: open indefinitely. Also the schema ``maximum`` so the dispatcher
#: rejects an over-long request before the handler runs; the clamp is
#: belt-and-suspenders for direct (test / other-handler) calls.
_MAX_TIMEOUT_SECONDS: Final[float] = 30.0
#: Hard cap on redirect hops followed before the walk halts with
#: ``too_many_redirects``. A fixed floor (not a param): a probe is a
#: reachability check, not a crawler, so an unbounded chain is never a
#: legitimate need (#1177 — one closed-set config, no tunable).
_MAX_REDIRECTS: Final[int] = 10
#: Methods the probe may issue. ``HEAD``/``GET`` only — a probe reads,
#: it never mutates. Enforced at the schema boundary (enum) so the
#: dispatcher rejects anything else before the handler runs.
_ALLOWED_METHODS: Final[tuple[str, ...]] = ("HEAD", "GET")

NET_HTTP_PROBE_PARAMETER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Absolute http:// or https:// URL to probe. The host "
                "(and every redirect hop's host) must be covered by "
                "MEHO_NETDIAG_PROBE_ALLOWLIST or the probe is refused "
                "before that host is dialed."
            ),
        },
        "method": {
            "type": "string",
            "enum": list(_ALLOWED_METHODS),
            "description": (
                "HTTP method — HEAD (default) or GET only. The response "
                "body is never returned regardless; GET only changes "
                "whether the origin sends one to size/hash."
            ),
        },
        "timeout_seconds": {
            "type": "number",
            "exclusiveMinimum": 0,
            "maximum": _MAX_TIMEOUT_SECONDS,
            "description": (
                "Total probe timeout in seconds across the whole "
                "redirect walk (default 5, max 30). A probe that does "
                "not complete in time returns reachable=false with "
                "reason='timeout'."
            ),
        },
        "host_header": {
            "type": "string",
            "minLength": 1,
            "description": (
                "Optional vhost override (hostname[:port]) for "
                "health-probing a vhost-routed service by IP before DNS "
                "exists: put the raw IP in `url` — that host is what is "
                "dialed AND allowlist-gated — and the virtual host name "
                "here. It is sent verbatim as the initial request's "
                "`Host:` header and, for an https `url`, also as the TLS "
                "SNI + certificate-verification name (so `verify` stays on "
                "against a cert pinned to the vhost, not the IP). It is "
                "NEVER dialed and NEVER widens the allowlist — the gate "
                "stays on the `url` host — and is dropped after the first "
                "redirect hop (each hop has its own canonical host)."
            ),
        },
    },
    "required": ["url"],
    "additionalProperties": False,
}

_NET_HTTP_PROBE_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reachable": {
            "type": "boolean",
            "description": (
                "True iff the probe reached an HTTP endpoint and got a "
                "response (including a blocked/too-many redirect, where "
                "the initial host answered). False on a connection-level "
                "failure or an allowlist refusal."
            ),
        },
        "reason": {
            "type": ["string", "null"],
            "description": (
                "Null on a clean terminal response; otherwise a code: "
                "invalid_url, blocked_redirect, too_many_redirects, "
                "timeout, dns_failure, refused, tls_error, unreachable. A "
                "URL host outside MEHO_NETDIAG_PROBE_ALLOWLIST is not "
                "reported here — it fails the dispatch with "
                "connector_probe_refused."
            ),
        },
        "status": {
            "type": ["integer", "null"],
            "description": "Final HTTP status code; null if no response was received.",
        },
        "headers": {
            "type": ["object", "null"],
            "description": (
                "Final response headers (lowercased names). Null if no "
                "response was received. The body is never included. When "
                "host_header is set these reflect the vhost-routed virtual "
                "host (the origin the Host: header selected), not a default "
                "vhost."
            ),
        },
        "redirect_chain": {
            "type": "array",
            "description": "One {url, status} entry per redirect hop, in order.",
            "items": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "status": {"type": "integer"},
                },
                "required": ["url", "status"],
                "additionalProperties": False,
            },
        },
        "tls": {
            "type": ["object", "null"],
            "description": (
                "TLS summary of the final connection (version, cipher, "
                "alpn, cert subject/issuer/not_after); null for plain "
                "HTTP or when no response was received. When host_header is "
                "set on an https probe the cert reflects the vhost-routed "
                "virtual host — SNI and certificate verification use the "
                "vhost name, not the dialed IP."
            ),
        },
        "timing_ms": {
            "type": ["number", "null"],
            "description": (
                "Wall-clock milliseconds from first dial to final response; null on early refusal."
            ),
        },
        "body_size": {
            "type": ["integer", "null"],
            "description": (
                "Byte length of the final response body (never the body itself); null if not read."
            ),
        },
        "body_sha256": {
            "type": ["string", "null"],
            "description": "SHA-256 hex digest of the final response body; null if not read.",
        },
        "final_url": {
            "type": ["string", "null"],
            "description": "The last URL actually dialed (audit-visible); null on early refusal.",
        },
        "blocked_redirect": {
            "type": ["string", "null"],
            "description": (
                "Host of a redirect target that was refused by the "
                "allowlist and never dialed; else null."
            ),
        },
        "error_detail": {
            "type": ["array", "null"],
            "description": (
                "On a connection-level failure, the actual mapped "
                "exception chain as innermost-first {type, message} "
                "entries (bounded, each message truncated) — the "
                "evidence behind the 'reason' code. Null on a clean "
                "response or a pre-dial rejection (invalid_url). Never "
                "contains the response body."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["type", "message"],
                "additionalProperties": False,
            },
        },
        "url": {"type": "string", "description": "The initially requested URL (audit-visible)."},
        "method": {"type": "string", "description": "The HTTP method issued."},
    },
    "required": [
        "reachable",
        "reason",
        "status",
        "headers",
        "redirect_chain",
        "tls",
        "timing_ms",
        "body_size",
        "body_sha256",
        "final_url",
        "blocked_redirect",
        "error_detail",
        "url",
        "method",
    ],
    "additionalProperties": False,
}

_NET_HTTP_PROBE_WHEN_TO_USE = (
    "Probe an HTTP(S) URL from the backplane's network vantage and "
    "report its status, response headers, redirect chain, TLS summary, "
    "and timing — e.g. 'what does GET https://svc/health return from "
    "here?', 'where does this URL redirect to?', 'what TLS version does "
    "the endpoint negotiate?'. A non-mutating reachability/identity "
    "probe: the response body is never returned (only its size and "
    "SHA-256), every redirect hop is re-checked against the probe "
    "allowlist before it is followed, and a failed/blocked probe is a "
    "normal result, not an error. The URL host must be inside "
    "MEHO_NETDIAG_PROBE_ALLOWLIST; one that is not fails with "
    "connector_probe_refused rather than reporting a (false) unreachable "
    "URL. To health-probe a vhost-routed service by IP before DNS exists "
    "(a strict-vhost appliance behind a NAT-alias IP), put the IP in the "
    "URL and the virtual host in the optional host_header param — the "
    "allowlist still gates only the dialed IP."
)

_NET_HTTP_PROBE_LLM_INSTRUCTIONS: dict[str, Any] = {
    "when_to_use": (
        "Use to confirm HTTP(S) reachability, identity, redirect target, "
        "or TLS posture of a URL from the backplane before assuming a "
        "connectivity, redirect, or certificate problem. Read-only: the "
        "response body is discarded (only size/hash reported), and a "
        "redirect to a non-allowlisted host is refused, not followed."
    ),
    "parameter_hints": {
        "url": "Required. Absolute http:// or https:// URL. Host must be allowlisted for probing.",
        "method": "Optional. HEAD (default) or GET only.",
        "timeout_seconds": "Optional. Total timeout across the redirect walk (default 5, max 30).",
        "host_header": (
            "Optional vhost override (hostname[:port]) to probe a "
            "vhost-routed service by IP: put the IP in url (dialed + "
            "allowlist-gated), the vhost here (sent as Host: and, for "
            "https, as TLS SNI + cert-verify name). Never dialed, never "
            "widens the allowlist, dropped after the first redirect hop."
        ),
    },
    "output_shape": (
        "On a clean terminal response: {'reachable': true, 'reason': "
        "null, 'status': <int>, 'headers': {...}, 'redirect_chain': "
        "[{'url', 'status'}...], 'tls': {...}|null, 'timing_ms': "
        "<float>, 'body_size': <int>, 'body_sha256': <hex>, 'final_url': "
        "<str>, 'blocked_redirect': null, 'url': <str>, 'method': <str>}. "
        "A redirect to a non-allowlisted host: reachable=true, "
        "reason='blocked_redirect', blocked_redirect=<host>, and the "
        "host is never dialed. A connection failure: reachable=false "
        "with reason (timeout|dns_failure|refused|tls_error|unreachable) "
        "plus error_detail (innermost-first [{type, message}], bounded) as "
        "the evidence behind reason. Every case is status=ok. Never a "
        "body. A URL host outside MEHO_NETDIAG_PROBE_ALLOWLIST is NOT a "
        "reading: the op fails with error_code='connector_probe_refused' "
        "and nothing was dialed."
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


def _result(
    *,
    url: str,
    method: str,
    reachable: bool,
    reason: str | None,
    status: int | None = None,
    headers: dict[str, str] | None = None,
    redirect_chain: list[dict[str, Any]] | None = None,
    tls: dict[str, Any] | None = None,
    timing_ms: float | None = None,
    body_size: int | None = None,
    body_sha256: str | None = None,
    final_url: str | None = None,
    blocked_redirect: str | None = None,
    error_detail: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Build the flat result payload (every key present, nulls where N/A).

    Deliberately has **no** ``body`` key — the response body is never
    captured, only ``body_size`` / ``body_sha256``. The shape matches
    :data:`_NET_HTTP_PROBE_RESPONSE_SCHEMA`.
    """
    return {
        "reachable": reachable,
        "reason": reason,
        "status": status,
        "headers": headers,
        "redirect_chain": redirect_chain if redirect_chain is not None else [],
        "tls": tls,
        "timing_ms": timing_ms,
        "body_size": body_size,
        "body_sha256": body_sha256,
        "final_url": final_url,
        "blocked_redirect": blocked_redirect,
        "error_detail": error_detail,
        "url": url,
        "method": method,
    }


def _elapsed_ms(started: float) -> float:
    return (time.perf_counter() - started) * 1000.0


def _headers_dict(headers: httpx.Headers) -> dict[str, str]:
    """Flatten response headers to a lowercased name→value dict.

    ``httpx.Headers`` lowercases names already; multi-valued headers are
    comma-joined by :meth:`httpx.Headers.items` semantics. No body, no
    cookies-as-secrets concern beyond what the origin already sent in the
    clear on a HEAD/GET.
    """
    return {name.lower(): value for name, value in headers.items()}


def _rdn(rdn_seq: Any) -> str | None:
    """Flatten an ``ssl`` RDN sequence (subject/issuer) to a string.

    ``getpeercert()`` renders these as a tuple of tuples of
    ``(key, value)`` pairs; join to a compact ``k=v, ...`` form. Returns
    ``None`` when the cert carried none (e.g. an unvalidated peer).
    """
    if not rdn_seq:
        return None
    parts: list[str] = []
    for rdn in rdn_seq:
        for pair in rdn:
            if len(pair) == 2:
                parts.append(f"{pair[0]}={pair[1]}")
    return ", ".join(parts) if parts else None


def _tls_summary(response: httpx.Response) -> dict[str, Any] | None:
    """Summarise the TLS of *response*'s connection, or ``None`` for plain HTTP.

    Reads the live ``ssl.SSLObject`` off httpx's ``network_stream``
    response extension *before* the body is consumed / the connection is
    closed. Never returns the raw cert or any private material — only the
    negotiated version, cipher, ALPN, and the peer cert's subject /
    issuer / expiry, which is public identity information.
    """
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    ssl_object = stream.get_extra_info("ssl_object")
    if not isinstance(ssl_object, ssl.SSLObject):
        return None
    cipher = ssl_object.cipher()
    cert = ssl_object.getpeercert() or {}
    return {
        "version": ssl_object.version(),
        "cipher": cipher[0] if cipher else None,
        "alpn": ssl_object.selected_alpn_protocol(),
        "subject": _rdn(cert.get("subject")),
        "issuer": _rdn(cert.get("issuer")),
        "not_after": cert.get("notAfter"),
    }


async def _consume_body_size_and_hash(response: httpx.Response) -> tuple[int, str]:
    """Stream the response body only to measure it — never to hold it.

    Iterates the byte stream chunk-by-chunk, accumulating **only** the
    running length and a SHA-256, so the full body is never materialised
    in memory or returned. This is the anti-exfil floor: the caller gets
    size + hash, never content. A HEAD response carries no body, so this
    returns ``(0, <sha256 of empty>)``.
    """
    hasher = hashlib.sha256()
    size = 0
    async for chunk in response.aiter_bytes():
        size += len(chunk)
        hasher.update(chunk)
    return size, hasher.hexdigest()


#: Priority order for transport-failure reason codes when one failure
#: surfaces several inner causes at once (e.g. a dual-stack probe whose
#: IPv6 attempt was refused while the IPv4 attempt hit a TLS error). The
#: most specific/actionable code wins; ``unreachable`` is the implicit
#: floor returned when nothing above matches.
_TRANSPORT_REASON_PRIORITY: Final[tuple[str, ...]] = (
    "tls_error",
    "dns_failure",
    "refused",
    "timeout",
)
#: Bounds on the ``error_detail`` evidence list — at most this many
#: innermost exceptions, each message truncated to this many characters.
_MAX_ERROR_DETAIL_ENTRIES: Final[int] = 6
_MAX_ERROR_DETAIL_MESSAGE_CHARS: Final[int] = 200


def _iter_exception_tree(exc: BaseException) -> Iterator[BaseException]:
    """Yield *exc* and every exception reachable from it, each once.

    Follows two edges: the ``__cause__`` chain (``raise X from Y`` — how
    httpcore/httpx re-wrap the underlying OS/TLS error) and, for an
    :class:`ExceptionGroup` / :class:`BaseExceptionGroup`, its
    ``.exceptions`` children (PEP 654). anyio raises
    ``OSError("All connection attempts failed") from ExceptionGroup(...)``
    on a multi-address (happy-eyeballs) connect, so the real per-attempt
    errors live in the group's children, not on ``__cause__`` — walking
    both edges is what stops them collapsing to ``unreachable``. An
    ``id``-based seen-set guards against ``__cause__`` cycles.
    """
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        node = stack.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        if node.__cause__ is not None:
            stack.append(node.__cause__)
        if isinstance(node, BaseExceptionGroup):
            stack.extend(node.exceptions)


def _reason_for_transport_error(exc: httpx.TransportError, *, tls: bool) -> str:
    """Map an httpx transport error to a probe reason code.

    Walks the whole exception tree (``__cause__`` **and**
    ``ExceptionGroup`` children — see :func:`_iter_exception_tree`) and
    classifies each node by type, so DNS, TLS, and connection-refused
    failures each get a distinct code instead of collapsing to
    ``unreachable``. When several inner causes disagree the most
    specific/actionable wins, per :data:`_TRANSPORT_REASON_PRIORITY`
    (``tls_error`` > ``dns_failure`` > ``refused`` > ``timeout`` >
    ``unreachable``).

    TLS-phase failures that carry **no** ``ssl.SSLError`` are caught too:
    httpcore's ``start_tls`` maps ``anyio.EndOfStream`` and
    ``anyio.BrokenResourceError`` to ``ConnectError`` when the peer
    closes or sends an alert mid-handshake (verified against
    httpcore 1.0.9 / anyio 4.13.0). ``EndOfStream`` is start_tls-only,
    but ``BrokenResourceError`` is *also* mapped by ``connect_tcp`` — so
    it only reads ``tls_error`` when *tls* is set (the probe URL was
    ``https``); on plain HTTP there is no handshake to blame and it stays
    ``unreachable``.
    """
    reasons: set[str] = set()
    for node in _iter_exception_tree(exc):
        if isinstance(node, ssl.SSLError):
            reasons.add("tls_error")
        elif isinstance(node, socket.gaierror):
            reasons.add("dns_failure")
        elif isinstance(node, ConnectionRefusedError):
            reasons.add("refused")
        elif isinstance(node, TimeoutError):
            reasons.add("timeout")
        elif tls and isinstance(node, (anyio.EndOfStream, anyio.BrokenResourceError)):
            reasons.add("tls_error")
    if isinstance(exc, httpx.TimeoutException):
        reasons.add("timeout")
    for reason in _TRANSPORT_REASON_PRIORITY:
        if reason in reasons:
            return reason
    return "unreachable"


def _exc_type_name(exc: BaseException) -> str:
    """Readable type label for the evidence detail (``module.Qual``).

    Drops the noisy ``builtins.`` prefix so a refused connect reads
    ``ConnectionRefusedError`` rather than
    ``builtins.ConnectionRefusedError``, while a library type keeps its
    module (``anyio.EndOfStream``, ``httpx.ConnectError``) for
    unambiguous attribution.
    """
    cls = type(exc)
    if cls.__module__ in ("builtins", "__main__"):
        return cls.__qualname__
    return f"{cls.__module__}.{cls.__qualname__}"


def _error_detail(exc: BaseException) -> list[dict[str, str]]:
    """Bounded, innermost-first ``[{type, message}]`` of *exc*'s tree.

    The classifier collapses a failure to one closed-enum ``reason``;
    this rides alongside it so a caller sees the *actual* mapped
    exception chain without re-running the probe under instrumentation.
    Innermost-first (the leaf cause is the most actionable), capped at
    :data:`_MAX_ERROR_DETAIL_ENTRIES` entries with each message truncated
    to :data:`_MAX_ERROR_DETAIL_MESSAGE_CHARS` — exception type + message
    only, never the response body.
    """
    nodes = list(_iter_exception_tree(exc))
    nodes.reverse()
    detail: list[dict[str, str]] = []
    for node in nodes[:_MAX_ERROR_DETAIL_ENTRIES]:
        message = str(node)
        if len(message) > _MAX_ERROR_DETAIL_MESSAGE_CHARS:
            message = message[:_MAX_ERROR_DETAIL_MESSAGE_CHARS] + "..."
        detail.append({"type": _exc_type_name(node), "message": message})
    return detail


def _sni_from_host_header(host_header: str) -> str:
    """Strip an optional trailing ``:port`` off *host_header* for the SNI name.

    The ``Host:`` header carries ``hostname[:port]`` verbatim, but the TLS
    SNI extension (RFC 6066) — and the certificate-verification name
    httpcore derives from it — is the bare hostname. ``rpartition`` peels
    only a numeric trailing port, so a bare hostname passes through
    untouched.
    """
    host, sep, port = host_header.rpartition(":")
    if sep and port.isdigit():
        return host
    return host_header


def _host_header_build_kwargs(host_header: str | None, *, is_https: bool) -> dict[str, Any]:
    """``httpx.build_request`` kwargs that force the vhost ``Host:`` + TLS SNI.

    Empty when no ``host_header`` is given, so the request is built
    byte-identically to today (SNI / Host derive from the URL). When set,
    ``Host:`` carries the vhost verbatim (``hostname[:port]``) and — for an
    https initial URL — the ``sni_hostname`` extension makes the handshake
    offer the bare vhost hostname as SNI and verify the presented cert
    against it (httpcore derives ``server_hostname`` from that extension,
    the #2002/#2863 seam). The ``Host:`` header alone does **not** suffice
    for https: without the SNI override an IP-dialed probe with ``verify``
    on offers the IP as SNI and fails cert verification (``tls_error``).
    Applied to the **first hop only** by :func:`_walk_redirects`.
    """
    if not host_header:
        return {}
    kwargs: dict[str, Any] = {"headers": {"Host": host_header}}
    if is_https:
        kwargs["extensions"] = {"sni_hostname": _sni_from_host_header(host_header)}
    return kwargs


def _advance_or_halt(
    *,
    response: httpx.Response,
    request: httpx.Request,
    redirect_chain: list[dict[str, Any]],
    url: str,
    method: str,
    started: float,
) -> dict[str, Any] | tuple[str, str]:
    """Decide the next step for a ``3xx`` *response* in the redirect walk.

    Returns a terminal ``_result`` dict when the walk must **halt** — the
    redirect budget is exhausted (``too_many_redirects``), the ``Location``
    resolves to no follow-up request (treated as terminal), or the next
    host is refused by the allowlist (``blocked_redirect`` SSRF re-gate,
    never dialed) — or the ``(next_url, next_method)`` to dial when the
    redirect target is allowlisted. Appends the current hop to
    *redirect_chain*.
    """
    status = response.status_code
    redirect_chain.append({"url": str(request.url), "status": status})
    if len(redirect_chain) > _MAX_REDIRECTS:
        return _result(
            url=url,
            method=method,
            reachable=True,
            reason="too_many_redirects",
            status=status,
            redirect_chain=redirect_chain,
            timing_ms=_elapsed_ms(started),
            final_url=str(request.url),
        )
    # httpx's own correctly-built follow-up request (method downgrade on
    # 301/302/303, relative-Location resolution).
    next_request = response.next_request
    if next_request is None:
        # has_redirect_location but no resolvable next request (e.g.
        # malformed Location) — treat as terminal.
        return _result(
            url=url,
            method=method,
            reachable=True,
            reason=None,
            status=status,
            headers=_headers_dict(response.headers),
            redirect_chain=redirect_chain,
            tls=_tls_summary(response),
            timing_ms=_elapsed_ms(started),
            final_url=str(request.url),
        )
    next_host = next_request.url.host
    try:
        assert_probe_allowed(next_host)
    except ProbeNotAllowedError:
        # SSRF re-gate: the redirect target is refused and never dialed.
        # reachable=true (the prior host did answer), but the walk halts.
        _log.info(
            "net.http_probe.blocked_redirect",
            blocked_redirect=next_host,
            from_url=str(request.url),
        )
        return _result(
            url=url,
            method=method,
            reachable=True,
            reason="blocked_redirect",
            status=status,
            redirect_chain=redirect_chain,
            timing_ms=_elapsed_ms(started),
            final_url=str(request.url),
            blocked_redirect=next_host,
        )
    return str(next_request.url), next_request.method


async def _walk_redirects(
    *,
    client: httpx.AsyncClient,
    url: str,
    method: str,
    started: float,
    first_hop_build_kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Issue the request and walk redirects manually, re-gating each hop.

    The initial host has already been allowlist-checked by the caller.
    Each ``3xx`` is handed to :func:`_advance_or_halt`, which re-gates the
    **next** host through :func:`assert_probe_allowed` *before* it is
    dialed. Bounded by :data:`_MAX_REDIRECTS`. ``first_hop_build_kwargs``
    (the ``host_header`` vhost override, from :func:`_host_header_build_kwargs`)
    rides the **first hop only** — a redirect target has its own canonical
    host, so carrying a forced ``Host:``/SNI across hops would probe the
    wrong virtual host.
    """
    redirect_chain: list[dict[str, Any]] = []
    current_url = url
    current_method = method

    for hop_index in range(_MAX_REDIRECTS + 1):
        build_kwargs = first_hop_build_kwargs if hop_index == 0 else {}
        request = client.build_request(current_method, current_url, **build_kwargs)
        response = await client.send(request, stream=True)
        try:
            if response.has_redirect_location:
                outcome = _advance_or_halt(
                    response=response,
                    request=request,
                    redirect_chain=redirect_chain,
                    url=url,
                    method=method,
                    started=started,
                )
                if isinstance(outcome, dict):
                    return outcome
                current_url, current_method = outcome
                continue

            # Terminal (non-redirect) response: capture TLS before the
            # stream is consumed, then measure the body without holding it.
            tls = _tls_summary(response)
            body_size, body_sha256 = await _consume_body_size_and_hash(response)
            return _result(
                url=url,
                method=method,
                reachable=True,
                reason=None,
                status=response.status_code,
                headers=_headers_dict(response.headers),
                redirect_chain=redirect_chain,
                tls=tls,
                timing_ms=_elapsed_ms(started),
                body_size=body_size,
                body_sha256=body_sha256,
                final_url=str(request.url),
            )
        finally:
            await response.aclose()

    # Unreachable: the loop either returns a terminal/blocked result or
    # exceeds the budget (handled inside). Guard for exhaustiveness.
    return _result(
        url=url,
        method=method,
        reachable=True,
        reason="too_many_redirects",
        redirect_chain=redirect_chain,
        timing_ms=_elapsed_ms(started),
    )


async def net_http_probe(operator: Operator, target: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Probe an HTTP(S) URL and report status/headers/redirects/TLS/timing.

    Op-id: ``net.http_probe``. Synthetic typed op (no vendor connector,
    ``target`` is always ``None``). The dispatcher has validated the
    param schema, so ``url`` is present and ``method`` (if supplied) is
    ``HEAD``/``GET``.

    Flow: parse + allowlist-gate the initial host → open a fresh
    ``httpx.AsyncClient(follow_redirects=False)`` → walk redirects
    manually, re-gating every hop's host before following it → capture
    the terminal response's status/headers/TLS/timing and the body's
    size+hash (**never the body**). A refused / blocked / timed-out /
    DNS-failed / TLS-failed probe returns a structured payload with
    ``status="ok"`` (the return-failures contract); nothing is raised as
    a ``connector_*`` error. The returned dict carries the literal
    ``url`` and ``final_url`` so the durable audit row records what was
    probed.
    """
    url = str(params["url"])
    method = str(params.get("method", "HEAD")).upper()
    timeout = _clamp_timeout(params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
    host_header_raw = params.get("host_header")
    host_header = str(host_header_raw) if host_header_raw else None

    # Parse + validate the URL locally before any network work.
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return _result(url=url, method=method, reachable=False, reason="invalid_url")
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return _result(url=url, method=method, reachable=False, reason="invalid_url")

    # Gate the initial host BEFORE any socket opens. This gates the
    # **dialed** ``url`` host only — never ``host_header``, which is an
    # HTTP/TLS routing hint, is never dialed, and so can never widen the
    # allowlist. A refusal propagates (#2784): no request was issued, so
    # there is no observation to report — the dispatcher renders it as
    # ``connector_probe_refused``.
    assert_probe_allowed(parsed.host)
    first_hop_build_kwargs = _host_header_build_kwargs(
        host_header, is_https=parsed.scheme == "https"
    )

    started = time.perf_counter()
    # Fresh client per call (NOT HttpConnector's per-target pool);
    # follow_redirects=False so this handler owns the redirect decision
    # and can re-gate each hop. Default TLS verification (SSL_CERT_FILE /
    # chart trust bundle honoured natively) — a verify failure surfaces
    # as reason='tls_error', still status=ok.
    try:
        async with httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(timeout),
        ) as client:
            return await asyncio.wait_for(
                _walk_redirects(
                    client=client,
                    url=url,
                    method=method,
                    started=started,
                    first_hop_build_kwargs=first_hop_build_kwargs,
                ),
                timeout=timeout,
            )
    except (TimeoutError, httpx.TimeoutException) as exc:
        return _result(
            url=url,
            method=method,
            reachable=False,
            reason="timeout",
            timing_ms=_elapsed_ms(started),
            error_detail=_error_detail(exc),
        )
    except httpx.TransportError as exc:
        return _result(
            url=url,
            method=method,
            reachable=False,
            reason=_reason_for_transport_error(exc, tls=parsed.scheme == "https"),
            timing_ms=_elapsed_ms(started),
            error_detail=_error_detail(exc),
        )


async def register_net_http_probe_operations(
    *,
    embedding_service: EmbeddingService | None = None,
) -> None:
    """Upsert the ``net.http_probe`` typed op into ``endpoint_descriptor``.

    Queued onto the lifespan-driven registrar list by the ``net`` package
    ``__init__`` (via ``register_typed_op_registrar``) and run by
    :func:`~meho_backplane.operations.typed_register.run_typed_op_registrars`
    after the connector eager-import pass. Idempotent: a re-run against
    unchanged text is a no-op for the embedding pipeline. ``safe`` +
    ``requires_approval=False`` — the probe allowlist is the only floor.
    """
    await register_typed_operation(
        product="net",
        version="1.x",
        impl_id="net-probe",
        op_id="net.http_probe",
        handler=net_http_probe,
        group_key="probe",
        when_to_use=_NET_HTTP_PROBE_WHEN_TO_USE,
        summary="Probe an HTTP(S) URL's status/headers/redirects/TLS/timing without its body.",
        description=(
            "Issues a single HEAD/GET to an operator-named URL from the "
            "backplane and reports status, response headers, the "
            "redirect chain, a TLS summary, and timing — but never the "
            "response body (only body_size and body_sha256), so it is a "
            "reachability/identity probe, not a fetch. Every redirect "
            "hop's host is re-checked against MEHO_NETDIAG_PROBE_ALLOWLIST "
            "before it is followed; a redirect to a non-allowlisted host "
            "halts with blocked_redirect and is never dialed (open-redirect "
            "SSRF floor). The initial host must be inside the allowlist or "
            "the probe is refused before any socket opens (empty allowlist "
            "⇒ the connector is inert). A refused, timed-out, DNS-failed, "
            "TLS-failed, or redirect-blocked probe returns reachable "
            "with a reason code and status=ok — a failed probe is the "
            "product, never a connector error. A connection-level "
            "failure also carries error_detail: the actual mapped "
            "exception chain (innermost-first {type, message}, bounded) "
            "behind the reason code. An optional host_header "
            "(hostname[:port]) health-probes a vhost-routed service by IP "
            "before DNS exists: the IP goes in url and the virtual host in "
            "host_header, sent verbatim as the initial Host: header and — "
            "for an https url — as the TLS SNI + certificate-verification "
            "name (so verify stays on against a cert pinned to the vhost). "
            "The allowlist always gates the DIALED url host, never "
            "host_header (which is never dialed and cannot widen it); the "
            "override is dropped after the first redirect hop."
        ),
        parameter_schema=NET_HTTP_PROBE_PARAMETER_SCHEMA,
        response_schema=_NET_HTTP_PROBE_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "http"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_HTTP_PROBE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
