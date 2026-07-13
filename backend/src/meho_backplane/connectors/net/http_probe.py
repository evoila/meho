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
  empty ⇒ every probe refused (the connector is inert).
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
from typing import TYPE_CHECKING, Any, Final

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
                "not_in_probe_allowlist, invalid_url, blocked_redirect, "
                "too_many_redirects, timeout, dns_failure, refused, "
                "tls_error, unreachable."
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
                "response was received. The body is never included."
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
                "HTTP or when no response was received."
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
    "MEHO_NETDIAG_PROBE_ALLOWLIST."
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
        "with reason (timeout|dns_failure|refused|tls_error|unreachable "
        "|not_in_probe_allowlist). Every case is status=ok. Never a body."
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


def _reason_for_transport_error(exc: httpx.TransportError) -> str:
    """Map an httpx transport error to a probe reason code.

    Walks the exception's ``__cause__`` chain to the underlying OS/TLS
    error so DNS, TLS, and connection-refused failures each get a
    distinct code rather than collapsing to ``unreachable``.
    """
    cause: BaseException | None = exc
    seen: set[int] = set()
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLError):
            return "tls_error"
        if isinstance(cause, socket.gaierror):
            return "dns_failure"
        if isinstance(cause, ConnectionRefusedError):
            return "refused"
        cause = cause.__cause__
    # No recognised inner cause — classify by the httpx type.
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | httpx.TimeoutException):
        return "timeout"
    return "unreachable"


async def _walk_redirects(
    *,
    client: httpx.AsyncClient,
    url: str,
    method: str,
    started: float,
) -> dict[str, Any]:
    """Issue the request and walk redirects manually, re-gating each hop.

    The initial host has already been allowlist-checked by the caller.
    For every ``3xx`` with a ``Location``, this re-gates the **next**
    host through :func:`assert_probe_allowed` *before* dialing it; a
    non-allowlisted target halts the walk with a ``blocked_redirect``
    result and is never dialed. Bounded by :data:`_MAX_REDIRECTS`.
    """
    redirect_chain: list[dict[str, Any]] = []
    current_url = url
    current_method = method

    for _hop in range(_MAX_REDIRECTS + 1):
        request = client.build_request(current_method, current_url)
        response = await client.send(request, stream=True)
        try:
            status = response.status_code
            if response.has_redirect_location:
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
                # httpx's own correctly-built follow-up request (method
                # downgrade on 301/302/303, relative-Location resolution).
                next_request = response.next_request
                if next_request is None:
                    # has_redirect_location but no resolvable next request
                    # (e.g. malformed Location) — treat as terminal.
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
                    # SSRF re-gate: the redirect target is refused and
                    # never dialed. reachable=true (the prior host did
                    # answer), but the walk halts here.
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
                current_url = str(next_request.url)
                current_method = next_request.method
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
                status=status,
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

    # Parse + validate the URL locally before any network work.
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, ValueError):
        return _result(url=url, method=method, reachable=False, reason="invalid_url")
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return _result(url=url, method=method, reachable=False, reason="invalid_url")

    # Gate the initial host BEFORE any socket opens.
    try:
        assert_probe_allowed(parsed.host)
    except ProbeNotAllowedError:
        _log.info("net.http_probe.refused", host=parsed.host, reason="not_in_probe_allowlist")
        return _result(url=url, method=method, reachable=False, reason="not_in_probe_allowlist")

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
                _walk_redirects(client=client, url=url, method=method, started=started),
                timeout=timeout,
            )
    except (TimeoutError, httpx.TimeoutException):
        return _result(
            url=url,
            method=method,
            reachable=False,
            reason="timeout",
            timing_ms=_elapsed_ms(started),
        )
    except httpx.TransportError as exc:
        return _result(
            url=url,
            method=method,
            reachable=False,
            reason=_reason_for_transport_error(exc),
            timing_ms=_elapsed_ms(started),
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
            "product, never a connector error."
        ),
        parameter_schema=NET_HTTP_PROBE_PARAMETER_SCHEMA,
        response_schema=_NET_HTTP_PROBE_RESPONSE_SCHEMA,
        tags=["net", "probe", "read", "diagnostics", "http"],
        safety_level="safe",
        requires_approval=False,
        llm_instructions=_NET_HTTP_PROBE_LLM_INSTRUCTIONS,
        embedding_service=embedding_service,
    )
