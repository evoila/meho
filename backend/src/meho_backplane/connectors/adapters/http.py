# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: file-size — pre-existing shared-transport module
# (671 lines before this change). The Python 3.14 migration only clears
# VERIFY_X509_STRICT in _build_ca_pinned_ssl_context and documents why;
# splitting the module is separate refactor work, out of scope here.

"""Abstract HTTP-API connector with shared transport plumbing.

Every HTTP-API connector (vSphere, NSX, Harbor, Hetzner Robot, etc.)
inherits :class:`HttpConnector` and overrides ``auth_headers()`` plus the
three ABC methods (``fingerprint``, ``probe``, ``execute``).

**Retry policy:** Three retries on idempotent verbs (GET, HEAD, OPTIONS)
with exponential backoff (0.5 s → 1 s → 2 s). The ``_request_json`` helper
carries the retry decorator; non-idempotent callers must bypass it.
``_retryable`` allows retries only on connection errors and 5xx responses —
4xx responses represent caller/auth errors that retrying would not fix.

**Client pooling:** Each :class:`HttpConnector` instance owns a dict of
``httpx.AsyncClient`` keyed by the tenant-unique
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``). The client is created lazily on first use and reused
across all operations against the same target. Keying on ``target.name``
alone would collide two same-named targets in different tenants — target
names are unique only per ``(tenant_id, name)`` — and since each pooled
client is host-bound via ``base_url``, the collision would route one
tenant's request to the other tenant's host and leak credentials across
the tenant boundary (evoila/meho#1682).

**Cert-bundle / TLS-trust support:** httpx honours ``SSL_CERT_FILE``
natively. The per-target TLS trust has three states, in precedence order:

1. **CA-pin** (``tls_ca_pin`` set, evoila/meho#1784) — the secure path.
   The client is built with a context from
   :func:`_build_ca_pinned_ssl_context`:
   :func:`ssl.create_default_context` plus
   :meth:`~ssl.SSLContext.load_verify_locations` ``(cadata=<pem>)``, which
   **keeps** ``CERT_REQUIRED`` + ``check_hostname`` ON, so chain and
   hostname are still enforced — now also trusting the pinned CA (the
   govc-``-thumbprint`` pattern). Takes precedence over
   ``verify_tls=False`` (the two are mutually exclusive at the API layer).
2. **Insecure opt-out** (``verify_tls=False``, evoila/meho#1774) — the
   audited last resort. The client is built with an explicit insecure
   :class:`ssl.SSLContext` (``check_hostname`` off, ``CERT_NONE``) so it
   can reach a self-signed / internal-CA appliance with no pin. Per-target,
   never global, and loud — see :func:`_insecure_ssl_context` and the WARN
   emitted at client construction.
3. **Default** (``verify_tls=True``, no pin) — the transport is built with
   ``verify=True``, so the global ``SSL_CERT_FILE`` / chart-trust-bundle
   path (evoila/meho#209) is in effect unchanged (httpx's
   ``create_ssl_context(verify=True, trust_env=True)`` reads it, exactly
   as an unconfigured client would).

Each resolved TLS context is carried by the per-target pinning
**transport** (:func:`build_port_pinned_async_transport`), not passed as a
client ``verify=`` argument, because the client is handed an explicit
``transport=`` that enforces destination pinning at the socket: ``connect_tcp`` re-screens the host
and dials only a guard-validated address from that same resolution, so a
resolver change between the guard's screen and the socket connect cannot
reach a blocked destination. Only the TCP target is rewritten — TLS SNI,
certificate verification, and the ``Host:`` header still use the
original hostname.

**Client pool key:** the pool is keyed on
:func:`~meho_backplane.connectors._shared.cache_key.target_cache_key`
(``(tenant_id, id)``) **plus** :meth:`HttpConnector.extra_cache_dimensions`
(``(verify_tls, ca_pin_digest, base_url)``), i.e.
``(tenant_id, id, verify_tls, ca_pin_digest, base_url)``. Appending the
three construction-time dimensions keeps the ``(tenant_id, id)``
tenant-isolation prefix intact (evoila/meho#1682/#1642) while ensuring a
PATCH that flips ``verify_tls``, rotates ``tls_ca_pin``, **or** changes
what :meth:`_base_url` resolves (``extras.scheme`` / host / port,
evoila/meho#2873) is not served the stale pooled client built under the
previous material. Both a client's ``verify`` context and its ``base_url``
are baked into the ``httpx.AsyncClient`` at construction, so both must be
in the key. The pin digest is the empty string when unpinned, so an
unpinned target's key is unchanged in its pin slot from the #1781 shape.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import hashlib
import os
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any, ClassVar, cast

import anyio
import httpx
import structlog
from httpx._decoders import SUPPORTED_DECODERS
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from meho_backplane.auth.operator import Operator
from meho_backplane.auth.principal_block import raise_if_send_refused
from meho_backplane.auth.principal_block_run import mark_request_sent
from meho_backplane.connector_packages import connector_package_of
from meho_backplane.connectors._shared.cache_key import (
    DEFAULT_PORTS,
    effective_scheme,
    target_cache_key,
)
from meho_backplane.connectors._shared.dial_guard import screen_dial_addresses
from meho_backplane.connectors._shared.log_host import (
    UNPARSEABLE_ADDRESS,
    UNREADABLE_LOCATION_ERRORS,
    host_for_log,
    unreadable_location_error,
)
from meho_backplane.connectors._shared.login_mark import (
    in_login_request,
    login_request,
    mark_as_login,
)
from meho_backplane.connectors._shared.pinned_transport import build_port_pinned_async_transport
from meho_backplane.connectors.base import Connector
from meho_backplane.flight_recorder import capture as flight_recorder_capture
from meho_backplane.targets.ssrf_guard import (
    TargetDestinationBlockedError,
    assert_public_destination_async,
)

logger = structlog.get_logger(__name__)


class SsrfBlockedError(httpx.ConnectError):
    """Dispatch refused: the target resolves to a non-public address.

    Connect-time arm of the target SSRF guard
    (:mod:`meho_backplane.targets.ssrf_guard`): raised by
    :meth:`HttpConnector._http_client` *before* any pooled client is
    built or request issued, when the stored ``host`` is — or now
    resolves to — a private / loopback / link-local (``169.254.0.0/16``
    metadata) / reserved address that the operator-configured
    ``MEHO_TARGET_SSRF_ALLOWLIST`` does not exempt. Re-screening the
    **resolved** address here (not just the create-time literal) closes
    the DNS-rebind window between create and dispatch.

    Subclasses :class:`httpx.ConnectError` so the dispatcher's existing
    ``ConnectError`` arm flattens it into the structured
    ``connector_error`` shape carrying the guard's message — no new
    dispatcher plumbing. Explicitly excluded from :func:`_retryable`:
    the rejection is deterministic policy, not a transient network
    failure, so retrying would only re-run the DNS lookup.
    """


class ResponseTooLargeError(Exception):
    """Dispatch refused: a vendor response body exceeded the byte cap.

    Raised by :func:`_read_capped_json_response` when a vendor response —
    declared via ``Content-Length`` or measured while streaming — exceeds
    :data:`_MAX_RESPONSE_BYTES`, *before* the body (or its parsed object
    graph) is materialised. The shared generic-connector transport
    otherwise buffers every vendor response fully into memory, so a single
    oversized body — from a compromised/malicious appliance, a MITM on a
    ``verify_tls=false`` channel, or a pathologically large legitimate
    listing — could exhaust the single-replica pod's heap and OOM-kill it.

    A plain :class:`Exception` (not an :class:`httpx.HTTPError`): it is a
    policy rejection, not a transport fault, so it flattens through the
    dispatcher's generic ``except Exception`` arm into the structured
    ``connector_error`` shape carrying this message. Being outside the
    ``httpx.ConnectError`` / ``httpx.HTTPStatusError`` families, it is
    automatically excluded from :func:`_retryable` (a deterministic
    over-cap verdict never resolves on replay), with no explicit exclusion
    needed.
    """


# Forward declaration — replaced with `from meho_backplane.targets import Target`
# once G0.3 lands the Target model.
type Target = Any

_IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _build_insecure_ssl_context() -> ssl.SSLContext:
    """Return a one-shot insecure context (verification disabled).

    Reuses the in-tree idiom (``backend/tests/acceptance/_vcsim.py``):
    start from :func:`ssl.create_default_context`, then disable
    ``check_hostname`` **before** dropping ``verify_mode`` to
    :data:`ssl.CERT_NONE`. The order is load-bearing — assigning
    ``CERT_NONE`` while ``check_hostname`` is still enabled raises
    ``ValueError`` on Python 3.12 ("Cannot set verify_mode to CERT_NONE
    when check_hostname is enabled.").
    """
    ctx = ssl.create_default_context()  # NOSONAR(S5527) — per-target verify_tls=false opt-out (default TRUE); WARN-logged + audited; tls_ca_pin is the secure supersession  # noqa: E501  # fmt: skip
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # NOSONAR(S4830) — same justification; see module docstring TLS-trust precedence  # noqa: E501  # fmt: skip
    return ctx


# Built once and shared across every insecure dispatch in the process.
# An ``SSLContext`` is safe to share between clients; it is never mutated
# after construction, so a single cached instance avoids rebuilding the
# context (and re-reading the default trust store it starts from) on each
# verify_tls=False target.
_INSECURE_SSL_CONTEXT = _build_insecure_ssl_context()


def _insecure_ssl_context() -> ssl.SSLContext:
    """Return the process-wide cached insecure :class:`ssl.SSLContext`."""
    return _INSECURE_SSL_CONTEXT


def _build_ca_pinned_ssl_context(ca_pem: str) -> ssl.SSLContext:
    """Return a context that trusts *ca_pem* while keeping verification ON.

    The **secure** supersession of the insecure context (evoila/meho#1784):
    start from :func:`ssl.create_default_context` -- which leaves
    ``check_hostname=True`` and ``verify_mode=CERT_REQUIRED`` -- and
    :meth:`~ssl.SSLContext.load_verify_locations` the per-target CA/cert
    PEM into its trust store via the ``cadata`` parameter. Crucially,
    ``load_verify_locations`` does **not** touch ``check_hostname`` or
    ``verify_mode`` (verified against the installed CPython ``ssl``),
    so the returned context still enforces chain **and** hostname -- now
    additionally trusting the pinned CA. This is the govc-``-thumbprint``
    pattern: trust *this specific* appliance's self-signed / internal-CA
    cert without weakening verification (contrast ``verify_tls=false``,
    which drops both).

    The pin is added *on top of* the default system trust store (the
    context starts from ``create_default_context``), so a pinned target
    still trusts public CAs too -- the pin is additive, not a replacement.

    ``VERIFY_X509_STRICT`` is cleared because CPython 3.13 added it (and
    ``VERIFY_X509_PARTIAL_CHAIN``) to ``create_default_context``'s default
    ``verify_flags`` (python/cpython#107361). Strict mode enforces RFC 5280
    to the letter -- notably requiring an Authority Key Identifier on
    non-self-signed certs -- which is exactly the conformance the
    self-signed / internal-CA appliance certs this function exists to trust
    tend to lack. Leaving it on would reject, with an opaque
    ``Missing Authority Key Identifier``, pinned targets that verify fine
    today; the upstream docs call this out as "a small amount of
    incompatibility with older X.509 certificates". Chain and hostname
    verification are untouched -- this restores the pre-3.13 pinning
    semantics, it does not weaken them. ``VERIFY_X509_PARTIAL_CHAIN`` is
    deliberately left on: letting path validation terminate at the pinned
    cert is precisely the intent of a pin.

    The PEM is validated at the API boundary
    (:func:`meho_backplane.targets.schemas.validate_ca_pin_pem`) before it
    is ever persisted, so by the time it reaches here it loads cleanly; a
    malformed pin would have been a 422 at create/update time, never an
    opaque dispatch failure.
    """
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    ctx.load_verify_locations(cadata=ca_pem)
    return ctx


def _ca_pin_digest(ca_pem: str | None) -> str:
    """Return a stable pool-key token identifying *ca_pem* (or ``""``).

    A short SHA-256 hex digest of the PEM, or the empty string when there
    is no pin. Seeds the client-pool cache key
    (:meth:`HttpConnector.extra_cache_dimensions`) so a target whose pinned
    CA *changes* (rotation) is not served the stale client built against
    the previous pin -- the same staleness guarantee ``verify_tls`` already
    gets, extended to the pin material. A digest (not the raw PEM) keeps
    the key compact and avoids carrying certificate bytes around as dict
    keys. It deliberately matches the digest the audit trail records
    (:func:`meho_backplane.api.v1.targets._ca_pin_digest`) so "a different
    pin" means the same thing to the pool and the audit log.
    """
    if not ca_pem:
        return ""
    return hashlib.sha256(ca_pem.encode("utf-8")).hexdigest()[:16]


def _pin_target_addresses(host: str, port: int | None = None) -> list[str] | None:
    """Connect-time screen for the pinned transport, in ``SsrfBlockedError`` terms.

    Wraps :func:`~meho_backplane.connectors._shared.dial_guard.screen_dial_addresses`
    (the shared connect-time address check, which also refuses a host name
    that does not resolve) so a block raised from *inside* the transport's
    ``connect_tcp`` surfaces as :class:`SsrfBlockedError` — an
    ``httpx.ConnectError`` subclass — exactly like the ``_http_client``
    pre-check does. httpx's transport exception mapping re-raises an
    unmapped exception unchanged, so an ``httpx.ConnectError`` subclass
    reaches the dispatcher's existing ``ConnectError`` arm (flattened into
    the structured ``connector_error`` shape) while a bare
    ``TargetDestinationBlockedError`` (a ``ValueError``) would escape
    unhandled. It is excluded from :func:`_retryable` for the same reason
    the pre-check's rejection is: the verdict is deterministic policy.
    """
    try:
        return screen_dial_addresses(host, port)
    except TargetDestinationBlockedError as exc:
        raise SsrfBlockedError(str(exc)) from exc


def _retryable(exc: BaseException) -> bool:
    """Retry on connection errors and 5xx; never on 4xx."""
    if isinstance(exc, SsrfBlockedError):
        # Deterministic SSRF-guard rejection, not a transient network
        # failure — retrying cannot change the verdict.
        return False
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return 500 <= exc.response.status_code < 600
    return False


def _post_body_content_type(
    json_body: dict[str, Any] | None, data_body: dict[str, Any] | None
) -> str | None:
    """Content-type of the non-idempotent body actually sent (#3214).

    ``json=`` serialises ``application/json``; ``data=`` serialises the
    form-encoded shape OAuth2 token grants / session-login POSTs use. Returned
    so the flight-recorder redaction engine can decide parseability (a
    form-encoded body it cannot structurally redact fails closed).
    """
    if json_body is not None:
        return "application/json"
    if data_body is not None:
        return "application/x-www-form-urlencoded"
    return None


def json_payload_or_empty(resp: httpx.Response) -> dict[str, Any]:
    """Parse *resp*'s JSON body, mapping ``204`` / empty bodies to ``{}``.

    Shared success-path tail for :meth:`HttpConnector._request_json` and
    :meth:`HttpConnector._post_json` (and the contract-parity
    ``VcfAutomationConnector`` transport override). Several vendor write
    endpoints acknowledge success with ``204 No Content`` and no body —
    vCenter's ``POST /vcenter/vm/{vm}/power?action=<verb>``, the guest
    ``/guest/power`` actions, ``DELETE /vcenter/vm/{vm}``, and the
    session-revoke ``DELETE`` — where an unconditional ``resp.json()``
    raised :exc:`json.JSONDecodeError` *after the write had already taken
    effect at the vendor*, so a succeeded write surfaced as
    ``connector_error`` and envelope-driven automation retried an
    operation that had executed (#3082). A ``204`` — or any empty body —
    now returns a benign ``{}``; callers already treat the payload as
    optional shape. A non-empty body parses exactly as before, so
    200-with-JSON behaviour is byte-identical (including the pass-through
    of non-dict JSON payloads such as vCenter's bare session-token
    string).
    """
    if resp.status_code == httpx.codes.NO_CONTENT or not resp.content:
        return {}
    return resp.json()  # type: ignore[no-any-return]


#: Default ceiling on a single vendor response body the shared dispatch
#: transport will buffer, in bytes (100 MiB). Large enough for any realistic
#: JSON listing — full vCenter inventory / event pulls are single-digit to
#: low-tens of MiB, and set-shaped results are reduced to a handle *after*
#: parse (JSONFlux, dispatcher step 8) — while bounding a multi-GB transfer
#: from a compromised/malicious appliance, a ``verify_tls=false`` MITM, or a
#: pathological legitimate listing that would otherwise OOM the
#: single-replica pod. Mirrors the
#: streamed byte-cap idiom the spec-fetch (``openapi._MAX_SPEC_BYTES``) and
#: event-ingest (``events_ingest._read_capped_body``) paths already enforce;
#: this closes the same gap on the outbound-dispatch path.
_DEFAULT_MAX_RESPONSE_BYTES = 100 * 1024 * 1024


def _resolve_max_response_bytes() -> int:
    """Resolve the response-body cap, honouring an operator env override.

    ``MEHO_CONNECTOR_MAX_RESPONSE_BYTES`` (a positive integer byte count)
    tunes the cap per deployment — a tighter-memory pod can lower it. An
    unset, non-integer, or non-positive value falls back to
    :data:`_DEFAULT_MAX_RESPONSE_BYTES` so a typo never silently disables
    the guard.
    """
    raw = os.environ.get("MEHO_CONNECTOR_MAX_RESPONSE_BYTES")
    if raw is None:
        return _DEFAULT_MAX_RESPONSE_BYTES
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_MAX_RESPONSE_BYTES
    return value if value > 0 else _DEFAULT_MAX_RESPONSE_BYTES


#: Resolved once at import; the streaming guard reads the module global by
#: name at call time, so a test (or a reload after an env change) can rebind
#: it to a small value without allocating a real over-cap body.
_MAX_RESPONSE_BYTES = _resolve_max_response_bytes()


def _reject_oversize_content_length(resp: httpx.Response) -> None:
    """Fast-reject a response whose declared ``Content-Length`` exceeds the cap.

    Fires before a single body byte is read, so an honest oversized
    response costs nothing to refuse. A missing header falls through to the
    streaming running-total guard (the real backstop against an absent,
    understated, or content-encoded length); a malformed header is likewise
    left to that guard rather than trusted.
    """
    declared = resp.headers.get("content-length")
    if declared is None:
        return
    try:
        length = int(declared)
    except ValueError:
        return
    if length > _MAX_RESPONSE_BYTES:
        raise ResponseTooLargeError(
            f"vendor response Content-Length {length} exceeds the "
            f"{_MAX_RESPONSE_BYTES}-byte response-body cap on bytes on the wire"
        )


def _body_over_cap_error(ceiling: str) -> ResponseTooLargeError:
    return ResponseTooLargeError(
        f"vendor response body exceeded the {_MAX_RESPONSE_BYTES}-byte "
        f"response-body cap on {ceiling}"
    )


def _content_codings(resp: httpx.Response) -> list[str]:
    """Return the ``Content-Encoding`` values of a response, without ``identity``."""
    codings = (
        value.strip().lower()
        for value in resp.headers.get_list("content-encoding", split_commas=True)
    )
    return [coding for coding in codings if coding not in ("", "identity")]


def _reject_stacked_content_codings(codings: list[str]) -> None:
    """Refuse a response that names more than one content coding.

    httpx decodes every layer of one wire chunk in a single step, before the
    decoded-size check can run, so MEHO accepts at most one coding.
    """
    if len(codings) > 1:
        raise ResponseTooLargeError(
            f"vendor response names {len(codings)} content codings "
            f"({', '.join(codings)}); MEHO accepts at most one"
        )


def _rebuilt_headers(resp: httpx.Response, codings: list[str], body_size: int) -> httpx.Headers:
    """Copy the headers, without the encoding headers when the body was decoded.

    Only a non-empty body under one coding that httpx knows was decoded. An
    empty body (a HEAD answer) and a coding httpx does not know keep all
    headers, so the caller still sees the ``Content-Encoding`` of the body.
    """
    headers = resp.headers.copy()
    if body_size and len(codings) == 1 and codings[0] in SUPPORTED_DECODERS:
        del headers["content-encoding"]
        if "content-length" in headers:
            del headers["content-length"]
    return headers


class _WireCappedStream(httpx.AsyncByteStream):
    """Count the raw wire bytes of a response and stop past the cap.

    It sits under :meth:`~httpx.Response.aiter_bytes`, so every raw chunk is
    counted before httpx decodes it. A check in the decoded loop would miss
    a chunk that decodes to no output (for example padding after the end of
    a gzip stream), because ``aiter_bytes`` yields nothing for it.
    """

    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self._inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise _body_over_cap_error("bytes on the wire")
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


async def _note_request_sent(_request: httpx.Request) -> None:
    """Request hook of every connector client: the check right before a request goes out.

    It runs after the credentials are ready, and again for each request a
    redirect sends. When the login of the running call is blocked, the
    request is not sent (:func:`raise_if_send_refused`). Otherwise the
    running operation notes that it sends a request.
    """
    await raise_if_send_refused()
    mark_request_sent()


#: Upper time limit, in seconds, that a stop waits for the close of an answer.
_STOP_CLOSE_LIMIT_S = 5.0

#: Strong references to read tasks that outlive a stop, so the event loop
#: cannot collect one before its close ends.
_stopped_reads: set[asyncio.Future[Any]] = set()


def _stopped_read_done(task: asyncio.Future[Any]) -> None:
    _stopped_reads.discard(task)
    if not task.cancelled():
        # The caller already got the stop; an error of the read has no reader.
        task.exception()


#: True inside a read that :func:`_run_stop_safe` already protects, so a
#: nested call (the stream read calls the client's ``send``) adds no task.
_inside_stop_safe: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_inside_stop_safe", default=False
)


#: Marks a read whose scope was stopped before the work returned.
_NO_ANSWER: Any = object()


async def _close_when_done(
    task: asyncio.Future[Any], discard: Callable[[Any], Awaitable[None]]
) -> None:
    """Wait for a read whose caller has stopped waiting, then close its answer."""
    await asyncio.wait({task})
    if task.cancelled() or task.exception() is not None:
        return
    result = task.result()
    if result is not _NO_ANSWER:
        await discard(result)


def _keep_until_done(task: asyncio.Future[Any]) -> None:
    _stopped_reads.add(task)
    task.add_done_callback(_stopped_read_done)


async def _run_stop_safe[T](
    work: Callable[[], Awaitable[T]],
    *,
    discard: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """Run *work* so that a stop cannot cut the close of an answer.

    httpcore closes an answer or a connection inside ``AsyncShieldCancellation``.
    That shield holds against anyio cancel scopes, but not against a plain
    ``task.cancel()``. So *work* runs in its own task, inside an anyio cancel
    scope. A stop on the caller cancels that scope, which respects httpcore's
    shield, and the close runs to its end. Then the caller gets the stop
    (``CancelledError``).

    The work can also end with an answer just before the stop reaches the
    caller. The caller then never gets that answer, so *discard* (when given)
    closes it in its own task.
    """
    if _inside_stop_safe.get():
        return await work()
    scope = anyio.CancelScope()

    async def _inside_scope() -> Any:
        # The task runs in a copy of the caller's context, so this stays inside it.
        _inside_stop_safe.set(True)
        result: Any = _NO_ANSWER
        with scope:
            result = await work()
        return result

    task: asyncio.Future[Any] = asyncio.ensure_future(_inside_scope())
    _keep_until_done(task)
    try:
        # Not asyncio.shield: it logs an error when the read fails after a stop.
        await asyncio.wait({task})
    except asyncio.CancelledError:
        scope.cancel()
        waited = task
        if discard is not None:
            # Started before any await, so a second stop cannot skip it.
            waited = asyncio.ensure_future(_close_when_done(task, discard))
            _keep_until_done(waited)
        await asyncio.wait({waited}, timeout=_STOP_CLOSE_LIMIT_S)
        raise
    result = task.result()
    if result is _NO_ANSWER:
        raise RuntimeError("the read ended without an answer")
    return cast("T", result)


async def _next_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    """Return the next part of a body, or ``None`` at its end."""
    try:
        return await anext(chunks)
    except StopAsyncIteration:
        return None


class _StopSafeCloseStream(httpx.AsyncByteStream):
    """Read and close a streamed answer through :func:`_run_stop_safe`.

    The caller of ``client.stream(...)`` reads and closes the answer in its
    own task, after ``send`` returned. When a body read fails (for example
    after a time limit), httpcore closes the connection inside that read. A
    stop during either close would leave the connection open, so each read
    and the close run in a protected task too.
    """

    def __init__(self, inner: httpx.AsyncByteStream) -> None:
        self._inner = inner

    async def __aiter__(self) -> AsyncIterator[bytes]:
        chunks = aiter(self._inner)
        while (chunk := await _run_stop_safe(lambda: _next_chunk(chunks))) is not None:
            yield chunk

    async def aclose(self) -> None:
        await _run_stop_safe(self._inner.aclose)


async def _close_answer(response: httpx.Response) -> None:
    await response.aclose()


async def _stream_capped_json_response(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
    content: bytes | None = None,
) -> httpx.Response:
    """Issue the request with a streamed, byte-capped read of the response.

    Shared success-path front for :meth:`HttpConnector._request_json` and
    :meth:`HttpConnector._post_json`. The default non-streaming
    ``client.request(...)`` buffers the whole vendor body into memory before
    the caller can inspect its size; this streams instead
    (:meth:`httpx.AsyncClient.stream` -> the pooled
    :class:`_SameOriginRedirectClient`'s ``send`` override, so the
    same-origin redirect and destination-pinning transport behaviour is
    unchanged). An over-cap body raises :exc:`ResponseTooLargeError` before
    :func:`json_payload_or_empty` can parse it.

    The cap has two ceilings, both :data:`_MAX_RESPONSE_BYTES`. The first
    bounds the compressed transfer: ``Content-Length`` and
    :class:`_WireCappedStream` count the bytes on the wire. The stream
    wrapper checks every raw chunk before it is decoded, also a chunk that
    decodes to no output. The second bounds the decoded body: the loop iterates
    :meth:`~httpx.Response.aiter_bytes`, which decodes ``Content-Encoding``
    chunk by chunk, and stops as soon as the decoded total passes the cap.
    So a small compressed body that expands a great deal (a compression
    bomb) is refused too. The error message names the ceiling that was
    passed.

    httpx decodes one wire chunk in a single step. A wire chunk is at most
    64 KiB (the read size of httpcore), and gzip or deflate expands it at
    most about 1032 times, so one chunk of one coding decodes to at most
    about 64 MiB. Memory is bounded by the cap plus that one chunk. A
    response that names more than one coding is refused before its body is
    read, because httpx would decode all layers of a chunk in that one
    step. MEHO accepts at most one coding.

    Because the kept bytes are already decoded, the rebuilt response drops
    the ``Content-Encoding`` and ``Content-Length`` headers when the body
    was really decoded. An empty body (a HEAD answer) and a coding that
    httpx does not know keep all headers, so callers can still read
    ``Content-Encoding`` there. Otherwise httpx would decode the plain bytes
    a second time and raise :exc:`httpx.DecodingError`.

    An under-cap body is joined and re-materialised into a fully-read
    :class:`httpx.Response` carrying the original status, headers, request,
    and extensions, so every downstream step — the flight-recorder span,
    ``raise_for_status``, and ``json_payload_or_empty`` — sees a response
    byte-identical to the non-streamed path.
    """
    # The check right before the request goes out, also for a client that has
    # no request hook (a connector that builds its own client).
    await raise_if_send_refused()
    mark_request_sent()
    async with client.stream(
        method,
        path,
        params=params,
        json=json,
        data=data,
        content=content,
        headers=headers,
        extensions=extensions,
        timeout=timeout,
    ) as resp:
        # A HEAD answer has no body: its Content-Length is the size of the
        # resource (for example a large file), not of anything read here.
        codings = _content_codings(resp)
        if method.upper() != "HEAD":
            _reject_oversize_content_length(resp)
            _reject_stacked_content_codings(codings)
        if not isinstance(resp.stream, httpx.AsyncByteStream):
            raise RuntimeError("an async client returned a sync response stream")
        resp.stream = _WireCappedStream(resp.stream)
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > _MAX_RESPONSE_BYTES:
                raise _body_over_cap_error("decoded bytes")
            chunks.append(chunk)
        rebuilt = httpx.Response(
            status_code=resp.status_code,
            headers=_rebuilt_headers(resp, codings, total),
            content=b"".join(chunks),
            request=resp.request,
            extensions=resp.extensions,
        )
        rebuilt.history = resp.history
        return rebuilt


async def _read_capped_json_response(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
    timeout: Any = httpx.USE_CLIENT_DEFAULT,
    content: bytes | None = None,
) -> httpx.Response:
    """Issue the request with a streamed, byte-capped read (see the worker).

    The worker runs through :func:`_run_stop_safe`, so a stop that comes while
    an answer closes leaves no connection open and no pool slot taken. The
    client's ``send`` has the same protection, but the stream is read and
    closed after ``send`` returns, so this read needs its own. The caller
    still gets the stop.
    """
    return await _run_stop_safe(
        lambda: _stream_capped_json_response(
            client,
            method,
            path,
            params=params,
            json=json,
            data=data,
            headers=headers,
            extensions=extensions,
            timeout=timeout,
            content=content,
        )
    )


# Hard cap on the same-origin redirect chain followed by
# :class:`_SameOriginRedirectClient`. Vendor canonicalisation never chains
# more than one trailing-slash hop in practice; the cap is a cheap guard
# against a same-origin redirect loop (a reverse proxy that bounces
# ``/a`` -> ``/a/`` -> ``/a`` would otherwise spin until the read timeout).
# httpx's own default ``max_redirects`` is 20; this is stricter because the
# only redirects we ever follow are benign single-hop canonicalisations.
_MAX_SAME_ORIGIN_REDIRECTS = 5


# The scheme rule and the default ports live next to the credential cache
# key, which needs the same answer for the login host (#5033).
_DEFAULT_PORTS = DEFAULT_PORTS
_effective_scheme = effective_scheme


def _effective_port(url: httpx.URL) -> int | None:
    """Return *url*'s port, substituting the scheme default when implicit.

    :attr:`httpx.URL.port` is ``None`` when the URL omits the port, so a
    bare ``https://h/`` and an explicit ``https://h:443/`` would otherwise
    compare unequal. Both name the same origin, so collapse them onto the
    scheme's default port before comparison.
    """
    return url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    """Return ``True`` when *a* and *b* share scheme, host, and port.

    An origin is the ``(scheme, host, port)`` triple (RFC 6454). Default
    ports are normalised via :func:`_effective_port` so a vendor redirect
    that names the port explicitly (``https://h:443/api/session/``) is
    still recognised as same-origin against the implicit-port base URL
    ``HttpConnector._base_url`` builds (``https://h``) — otherwise the
    benign trailing-slash canonicalisation this client exists to preserve
    would be wrongly refused as cross-origin.
    """
    return (a.scheme, a.host, _effective_port(a)) == (
        b.scheme,
        b.host,
        _effective_port(b),
    )


def _refusal_reason(source: httpx.URL, location: httpx.URL | None) -> str:
    """Return one fixed word for why MEHO does not follow a redirect from *source* to *location*."""
    if location is None:
        return "unreadable address"
    if source.host != location.host:
        return "other host"
    if source.scheme != location.scheme:
        return "other scheme"
    return "other port"


def _log_redirect_not_followed(
    request: httpx.Request, location: str | None, *, reason: str
) -> None:
    """Warn that MEHO does not follow a redirect; *location* is a host name or a fixed mark."""
    logger.warning(
        "connector_redirect_not_followed_cross_origin",
        method=request.method,
        # Vendor-supplied addresses are logged as host name only.
        from_url=host_for_log(request.url),
        location=location,
        reason=reason,
    )


# A change (any method that is not a read) follows at most one redirect, and
# only a 307 / 308 to the same path (see :func:`_change_may_be_sent_again`).
_MAX_CHANGE_REDIRECTS = 1

#: The HTTP codes of a redirect.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


#: Set while :meth:`HttpConnector._request_json`,
#: :meth:`HttpConnector._post_json` or :meth:`HttpConnector._send_traced`
#: sends the operation's own request. Only then does the client put each
#: followed redirect into the trace, so login traffic stays out of it. The
#: operation's own request is never a login, also when it is sent inside a
#: :func:`login_request` block.
_operation_request: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "meho_operation_request", default=False
)


#: Set on the first request when a followed request fails: the method of
#: the request that failed (a GET after an OPTIONS that got a 303).
_FAILED_METHOD_EXTENSION = "meho_failed_method"


def _failed_method(exc: httpx.TransportError, sent_method: str) -> str:
    """The method of the request that failed: after a redirect it can differ from the sent one."""
    try:
        request = exc.request
    except RuntimeError:
        return sent_method
    failed = request.extensions.get(_FAILED_METHOD_EXTENSION)
    return failed if isinstance(failed, str) else request.method


def _encoded_path(url: httpx.URL) -> str:
    """The path of *url* as it is sent: still percent-encoded, without the query.

    :attr:`httpx.URL.path` decodes ``%2F``, so ``/a%2Fb`` and ``/a/b`` would
    look equal although they name different resources.
    """
    return url.raw_path.split(b"?", 1)[0].decode("ascii")


def _folded_path(url: httpx.URL) -> str:
    """The encoded path of *url* without one trailing slash, in lower case."""
    return _encoded_path(url).removesuffix("/").lower()


#: Set while MEHO sends an operation again after "the login ended": the
#: change was already sent once, so it follows no redirect
#: (:func:`change_sent_once_before`).
_change_sent_before: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "meho_change_sent_before", default=False
)


@contextlib.contextmanager
def change_sent_once_before() -> Iterator[None]:
    """Mark the calls inside as a repeat of a change that was already sent once.

    A change is sent at most twice. The dispatcher sends an operation again
    after "the login ended" only when its first send was one request. Inside
    this block the operation's own change request follows no redirect, so
    the repeat is the second and last send. The fresh login inside the block
    and every read follow a redirect on the same origin.
    """
    token = _change_sent_before.set(True)
    try:
        yield
    finally:
        _change_sent_before.reset(token)


@contextlib.contextmanager
def last_send_of_a_change() -> Iterator[None]:
    """Mark the change request sent inside as the second and last send.

    For a typed connector that sends a change once more itself, for example
    after a token refresh on a 401. The request inside follows no redirect,
    so the change reaches the target at most twice. Renew the token before
    this block, so a login follows a redirect on the same origin.
    """
    repeat = _change_sent_before.set(True)
    own = _operation_request.set(True)
    try:
        yield
    finally:
        _operation_request.reset(own)
        _change_sent_before.reset(repeat)


def _sent_address(url: httpx.URL) -> tuple[str, str, int | None, str, str]:
    """The address a request goes to: scheme, host, port, encoded path and query.

    The fragment and the login part (user name and password) are left out:
    HTTP sends neither as part of the address, so two URLs that differ only
    there reach the same resource.
    """
    return (
        url.scheme,
        url.host,
        _effective_port(url),
        _encoded_path(url),
        url.query.decode("ascii"),
    )


def _change_may_be_sent_again(
    status_code: int,
    sent: httpx.URL,
    location: httpx.URL,
    already_sent: set[tuple[str, str, int | None, str, str]],
) -> bool:
    """Whether a change may be sent once more to *location* after a redirect.

    Only a 307 or 308 (the target says: send the same request there), only to
    the path that was sent (apart from one trailing slash or letter case) with
    the same query, and never to an address that was already sent to. The
    compare uses the sent, still encoded URL, never a path template: a
    template would match a redirect to another object's id.
    """
    return (
        status_code in (307, 308)
        and _folded_path(sent) == _folded_path(location)
        and sent.query == location.query
        and _sent_address(location) not in already_sent
    )


class _SameOriginRedirectClient(httpx.AsyncClient):
    """An ``AsyncClient`` that follows redirects **only within one origin**.

    The shared pooled client must not transparently follow ``httpx``'s
    default redirect chain across origins: a malicious or misconfigured
    vendor appliance can answer a session-create / login ``POST`` with a
    ``3xx`` whose ``Location`` points at an attacker host, and ``httpx``
    will replay the request there. ``httpx`` strips the ``Authorization``
    header on a cross-origin hop, but it does **not** strip vendor-specific
    auth headers (NSX's ``X-XSRF-TOKEN``) nor — on a method-preserving
    ``307``/``308`` — the credential request body, so the login secret and
    session token leak to the redirect target (open-redirect SSRF /
    auth-token forwarding).

    This client is built with ``follow_redirects=False`` and re-implements
    a *bounded, same-origin-only* redirect loop in :meth:`send`. A legitimate
    same-origin canonicalisation (the vendor trailing-slash ``301`` that
    vcsim's ``/rest`` mount and some appliances emit) is still followed, so
    no vendor flow regresses.

    The redirect rule depends on the kind of request. A read (GET, HEAD,
    OPTIONS) and a named login (:func:`login_request`, or a request sent
    inside a connector's ``auth_headers``) follow redirects on the same
    origin up to the hop budget. When :attr:`change_rule` is on, every other
    request gets the change rule: at most two sends, and a second send only
    after a 307 or 308 to the same path with the same query. When it is off,
    every request follows redirects on the same origin. Any cross-origin
    ``Location`` terminates the loop and the ``3xx`` response is returned to
    the caller unfollowed — the request is **never** replayed off-origin, so
    no header or body crosses the origin boundary.
    """

    #: Whether a request that is neither a read nor a named login gets the
    #: change rule. :meth:`HttpConnector._build_http_client` sets it from
    #: :meth:`HttpConnector.uses_change_rule`.
    change_rule: bool = False

    async def send(
        self,
        request: httpx.Request,
        *,
        stream: bool = False,
        auth: Any = httpx.USE_CLIENT_DEFAULT,
        follow_redirects: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> httpx.Response:
        # Every request of the pooled client passes here, also a direct
        # ``client.request(...)``: a stop cannot cut the close of an answer
        # (after a time limit, or of a 3xx before the next hop).
        # A streamed answer is closed later by the caller; that close is
        # protected too.
        response = await _run_stop_safe(
            lambda: self._send_same_origin(request, stream, auth), discard=_close_answer
        )
        if self._is_unfollowed_login_redirect(response):
            await response.aclose()
            # Imported here: the error module imports the login helpers.
            from meho_backplane.connectors._shared.login_redirect import (
                SessionLoginRedirectError,
            )

            raise SessionLoginRedirectError(response.status_code)
        if stream and isinstance(response.stream, httpx.AsyncByteStream):
            response.stream = _StopSafeCloseStream(response.stream)
        return response

    def _is_unfollowed_login_redirect(self, response: httpx.Response) -> bool:
        """Whether a named login ends on a redirect that MEHO did not follow.

        Only with the switch ``redirect_on_change_result`` on, and only for a
        client with the change rule. Then the login ends with
        :class:`SessionLoginRedirectError` before the login code reads the
        answer. With the switch off the login code reads the answer itself.
        """
        if not (
            self.change_rule
            and response.status_code in _REDIRECT_STATUSES
            and in_login_request()
            and not _operation_request.get()
        ):
            return False
        from meho_backplane.settings import get_settings

        return get_settings().redirect_on_change_result

    def _build_redirect_request(
        self, request: httpx.Request, response: httpx.Response
    ) -> httpx.Request:
        # httpx builds the next request inside ``send`` and raises (closing the
        # answer) when it cannot read the Location address. Log the fixed mark
        # for that address, then raise an error of the same class with one
        # fixed text. The raise sits outside the ``except``, so the error
        # MEHO raises carries no other error as its context.
        try:
            return super()._build_redirect_request(request, response)
        except UNREADABLE_LOCATION_ERRORS as exc:
            _log_redirect_not_followed(request, UNPARSEABLE_ADDRESS, reason="unreadable address")
            error = unreadable_location_error(exc, request)
        raise error

    @staticmethod
    def _hop_span_start() -> flight_recorder_capture.SpanStart | None:
        """A trace start for a redirect hop, only inside a traced helper."""
        return flight_recorder_capture.span_start() if _operation_request.get() else None

    async def _send_same_origin(
        self, request: httpx.Request, stream: bool, auth: Any
    ) -> httpx.Response:
        # Always issue the underlying request without httpx's own redirect
        # following; this class owns the redirect decision so the
        # same-origin invariant cannot be undone by a per-call
        # ``follow_redirects=True`` slipping through a convenience method.
        first_request = request
        # A read and a named login follow a redirect on the same origin. Every
        # other request is a change, when this client has the change rule.
        is_login = in_login_request() and not _operation_request.get()
        is_change = (
            self.change_rule and request.method.upper() not in _IDEMPOTENT_METHODS and not is_login
        )
        if is_login:
            # The error handling reads the kind of the request from the request.
            mark_as_login(request)
        if is_change:
            # The repeated operation request follows no redirect.
            max_hops = 0 if _change_sent_before.get() else _MAX_CHANGE_REDIRECTS
        else:
            max_hops = _MAX_SAME_ORIGIN_REDIRECTS
        sent_urls = {_sent_address(request.url)}
        # Each answer that MEHO followed. The last answer carries them in
        # ``history`` (as httpx does), so a caller can tell that the request
        # it sees was not the first one sent.
        history: list[httpx.Response] = []
        hop_start = self._hop_span_start()
        response = await super().send(request, stream=stream, auth=auth, follow_redirects=False)
        while True:
            response.history = list(history)
            if not response.has_redirect_location:
                return response
            # ``next_request`` is httpx's own correctly-built follow-up
            # request (method downgrade on 301/302, body/headers carried on
            # 307/308) — reuse it rather than reconstructing the hop.
            next_request = response.next_request
            if next_request is None or not _same_origin(request.url, next_request.url):
                # Cross-origin (or unresolvable) redirect: refuse to
                # follow. Returning the 3xx unfollowed means no header or
                # body — and so no ``X-XSRF-TOKEN`` / credential — is ever
                # replayed to the off-origin ``Location``.
                location_header = response.headers.get("Location")
                _log_redirect_not_followed(
                    request,
                    host_for_log(location_header) if location_header is not None else None,
                    reason=_refusal_reason(
                        request.url, next_request.url if next_request is not None else None
                    ),
                )
                return response
            if is_change and (
                len(history) >= max_hops
                or not _change_may_be_sent_again(
                    response.status_code, request.url, next_request.url, sent_urls
                )
            ):
                # A change is sent at most twice, and only to the same path.
                # Any other redirect ends here; the dispatcher words the
                # result by what the status code means.
                logger.warning(
                    "connector_redirect_not_followed_on_change",
                    method=request.method,
                    status_code=response.status_code,
                    # Host names only, as for a redirect to another origin.
                    from_url=host_for_log(request.url),
                    location=host_for_log(next_request.url),
                )
                return response
            if len(history) >= max_hops:
                # Exhausted the same-origin hop budget — surface httpx's own
                # error so the caller sees a redirect loop rather than a
                # silent 3xx. Close the final 3xx first so its connection is
                # released back to the pool rather than held checked-out
                # until GC (a looping target would otherwise slowly starve
                # the pooled client).
                await response.aclose()
                raise httpx.TooManyRedirects(
                    f"exceeded the same-origin redirect budget ({_MAX_SAME_ORIGIN_REDIRECTS})",
                    request=first_request,
                )
            # One trace entry per request that was really sent, with its own
            # method: the helper that sent the request records the last one.
            # The body of a followed redirect is not recorded: it skips the
            # connector's own masking (``trace_response_body``), so it could
            # hold a secret in a field the shared redaction does not know.
            flight_recorder_capture.record_vendor_call(
                hop_start,
                method=request.method,
                request_headers=request.headers,
                response=response,
                response_body=b"",
            )
            await response.aclose()
            history.append(response)
            # The send below runs the client's request hook (``_note_request_sent``),
            # so a followed redirect passes the same block check as the first send.
            request = next_request
            if is_login:
                mark_as_login(request)
            sent_urls.add(_sent_address(request.url))
            hop_start = self._hop_span_start()
            response = await self._send_followed_hop(first_request, request, stream, auth)

    async def _send_followed_hop(
        self, first_request: httpx.Request, request: httpx.Request, stream: bool, auth: Any
    ) -> httpx.Response:
        """Send one followed same-origin hop; name *first_request* for the trace and errors.

        The address of a followed hop is the vendor's choice. So an error of
        the hop carries the request MEHO sent, and the answer carries its
        address for the trace. The hop request carries that address too, so
        the trace names it also when a later read of a streamed body fails.
        """
        request.extensions[flight_recorder_capture.FIRST_REQUEST_URL_EXTENSION] = first_request.url
        try:
            response = await super().send(request, stream=stream, auth=auth, follow_redirects=False)
        except httpx.TransportError as exc:
            first_request.extensions[_FAILED_METHOD_EXTENSION] = request.method
            exc.request = first_request
            raise
        response.extensions[flight_recorder_capture.FIRST_REQUEST_URL_EXTENSION] = first_request.url
        return response


#: The client class that knows the change rule, under a name that a test
#: which patches ``_SameOriginRedirectClient`` does not replace.
_ChangeRuleClient = _SameOriginRedirectClient


def _as_login(method: Callable[..., Awaitable[dict[str, str]]]) -> Any:
    """Wrap a connector's ``auth_headers`` so each request it sends is a login."""

    @functools.wraps(method)
    async def auth_headers(self: Any, *args: Any, **kwargs: Any) -> dict[str, str]:
        with login_request():
            return await method(self, *args, **kwargs)

    auth_headers._names_login = True  # type: ignore[attr-defined]
    return auth_headers


class HttpConnector(Connector):
    """Abstract HTTP-API connector with retry/timeout/cert-bundle support.

    Subclasses MUST override ``auth_headers()`` and the three ABC methods
    (``fingerprint``, ``probe``, ``execute``).
    """

    #: The ``httpx`` client class :meth:`_build_http_client` builds, or
    #: ``None`` for the shared :class:`_SameOriginRedirectClient`, which
    #: follows redirects on the same origin only. A connector whose vendor
    #: never redirects (Azure) sets a class that follows none, so a redirect
    #: on the same host can never carry a token to another path.
    _client_class: type[httpx.AsyncClient] | None = None

    #: A connector package sets this to ``True`` to say that it names each of
    #: its logins: it sends them inside :meth:`auth_headers` or inside
    #: :func:`login_request`. Then every other request of the package that is
    #: not a read gets the change rule (see :meth:`uses_change_rule`). A
    #: connector that comes with MEHO itself gets the change rule always.
    names_its_logins: ClassVar[bool] = False

    @classmethod
    def uses_change_rule(cls) -> bool:
        """Whether a request of this connector that is not a read or a login gets the change rule.

        Yes for a connector that comes with MEHO itself, and for a connector
        package that declares :attr:`names_its_logins`. A package that does
        not declare it keeps the rule of a read for every request: a login it
        sends outside ``auth_headers`` would otherwise end on a redirect.
        """
        return cls.names_its_logins or connector_package_of(cls.__module__) is None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        own = cls.__dict__.get("auth_headers")
        if own is not None and not getattr(own, "_names_login", False):
            cls.auth_headers = _as_login(own)  # type: ignore[method-assign]

    def __init__(self) -> None:
        # Keyed on the tenant-unique ``(tenant_id, id)`` tuple
        # (``target_cache_key``) plus ``extra_cache_dimensions`` (the
        # ``(verify_tls, ca_pin_digest, base_url)`` construction-time
        # dimensions), not ``target.name``: each pooled client is host-bound
        # via ``base_url``, and two tenants may legitimately own same-named
        # targets pointing at different hosts. Name-keying would route the
        # second tenant's request to the first tenant's host and leak
        # credentials across the boundary (evoila/meho#1682). The suffix
        # keeps a PATCH that flips ``verify_tls``, rotates the ``tls_ca_pin``,
        # or changes what ``_base_url`` resolves (``extras.scheme`` / host /
        # port, evoila/meho#2873) from being served the stale client built
        # under the old material.
        self._clients: dict[tuple[str, ...], httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()

    def extra_cache_dimensions(self, target: Target) -> tuple[object, ...]:
        """Return extra pool-key dimensions appended to ``target_cache_key``.

        The base appends three construction-time dimensions so a target
        whose transport material *changes* (via PATCH) gets a freshly built
        client rather than the stale one cached under the previous material.
        Both a client's ``verify`` context and its ``base_url`` are fixed at
        construction, so both must be in the key:

        1. the resolved ``verify_tls`` flag (evoila/meho#1781),
        2. a digest of the per-target ``tls_ca_pin`` CA material
           (evoila/meho#1784) — empty string when unpinned, so an
           unpinned target's key is unchanged from the #1781 shape in its
           pin slot, and
        3. the resolved :meth:`_base_url` (evoila/meho#2873). ``base_url``
           is baked into the ``httpx.AsyncClient`` at construction, so a
           PATCH changing anything ``_base_url`` reads — ``extras.scheme``
           (evoila/meho#2599), ``host``, or ``port`` — must rotate the pool
           entry exactly like a TLS-trust change does, or the warm pool
           keeps serving a client bound to the old URL forever. Keying on
           the *polymorphically resolved* URL (subclasses override
           :meth:`_base_url` — the Prometheus/Loki plain-HTTP variants,
           gcloud, github, vcf_automation) rather than on the raw
           ``extras.scheme`` is what captures Loki's default-``http``
           override — where ``scheme``-absent and ``scheme=https`` share a
           base scheme yet resolve to different URLs — without regressing
           Prometheus's silent coercion of an unrecognised scheme. An
           *invalid* ``extras.scheme`` therefore now fails loud at key
           derivation (``_effective_scheme`` raises) instead of silently
           serving the stale client.

        The append preserves the ``(tenant_id, id)`` prefix, so the
        cross-tenant isolation guarantee (evoila/meho#1682/#1642) is
        unaffected. Subclasses that reach into :attr:`_clients` directly
        MUST build their lookup key the same way
        (``target_cache_key(target) + self.extra_cache_dimensions(target)``)
        so they index the same entry the base created.
        """
        return (
            bool(getattr(target, "verify_tls", True)),
            _ca_pin_digest(getattr(target, "tls_ca_pin", None)),
            self._base_url(target),
        )

    def _client_cache_key(self, target: Target) -> tuple[str, ...]:
        """Return the full pooled-client key for *target*.

        ``target_cache_key`` (``(tenant_id, id)``) plus
        :meth:`extra_cache_dimensions`
        (``(verify_tls, ca_pin_digest, base_url)``). Stringified into a flat
        ``tuple[str, ...]`` so the key is hashable and a subclass that
        derives it the same way produces an identical tuple.
        """
        return target_cache_key(target) + tuple(
            str(dim) for dim in self.extra_cache_dimensions(target)
        )

    async def _http_client(self, target: Target) -> httpx.AsyncClient:
        """Return the per-target pooled client, creating it on first use.

        TLS-trust precedence, highest first:

        1. **CA-pin** (``tls_ca_pin`` set, evoila/meho#1784) — the secure
           path. Build a context that trusts the pinned CA while keeping
           ``CERT_REQUIRED`` + ``check_hostname`` ON. Takes precedence over
           ``verify_tls=false`` (and the API rejects the two together), so
           a pin can never be silently undone by a stray insecure flag.
        2. **Insecure opt-out** (``verify_tls=false``, evoila/meho#1781) —
           the audited last resort: an explicit insecure context (no chain,
           no hostname).
        3. **Default** (``verify_tls=true``, no pin) — pass **no**
           ``verify=`` argument so httpx keeps its default ``verify=True``
           and the global ``SSL_CERT_FILE`` / chart-trust-bundle path
           (evoila/meho#209) stays byte-identical to a connector with no
           TLS config at all.
        """
        # SSRF guard: re-screen the
        # *resolved* destination on every acquisition, before the pooled
        # client is built or served. The create/update schema validators
        # already rejected a non-public literal, but the stored value can
        # be a hostname whose DNS answer changed since create (rebind) or
        # was unresolvable then — this is the enforcement point. The
        # guard screens the httpx-normalized *dialed* host, never the
        # raw string, so a stored value that embeds URL structure —
        # including rows that predate the guard — is screened on the
        # host the transport actually reaches. Runs before the cache
        # lookup so a previously-pooled client for a now-rebinding
        # hostname is refused too.
        try:
            await assert_public_destination_async(str(target.host), self._dial_port(target))
        except TargetDestinationBlockedError as exc:
            raise SsrfBlockedError(str(exc)) from exc
        cache_key = self._client_cache_key(target)
        async with self._lock:
            if cache_key not in self._clients:
                self._clients[cache_key] = self._build_http_client(target)
            return self._clients[cache_key]

    def _build_http_client(self, target: Target) -> httpx.AsyncClient:
        """Construct a new pinned-transport client for *target* (no SSRF pre-check).

        The construction half of :meth:`_http_client` (TLS trust + the
        destination-pinned transport + same-origin redirects), split out so a
        connector that needs a **short-lived** client with its own cookie jar
        -- e.g. a login as a different principal than the pooled session --
        builds it identically via :meth:`_ephemeral_http_client`. Callers own
        the SSRF pre-check.

        The client follows a redirect only on the same origin. Its
        ``change_rule`` comes from :meth:`uses_change_rule`: when it is on, a
        request that is neither a read nor a named login follows only a 307
        or 308 to the same path (see :class:`_SameOriginRedirectClient`).
        """
        verify_tls = bool(getattr(target, "verify_tls", True))
        ca_pin = getattr(target, "tls_ca_pin", None)
        # Resolve the TLS-trust argument for the pinned transport.
        # ``True`` keeps httpx's default ``verify=True`` + the
        # global ``SSL_CERT_FILE`` path (evoila/meho#209)
        # byte-identical to a connector with no TLS config at all;
        # a CA-pin or the insecure opt-out supersedes it below.
        # The transport (not the client) carries this context now,
        # because the client is handed an explicit ``transport=``.
        verify_arg: ssl.SSLContext | bool = True
        if ca_pin:
            # Secure supersession (evoila/meho#1784): trust the
            # pinned CA while keeping CERT_REQUIRED + hostname
            # verification on. Built per pool entry (not module-
            # cached like the insecure context) because the PEM is
            # per-target; the pool key carries the pin digest so the
            # context is rebuilt only when the pin actually changes,
            # not per request. Logged at INFO (not WARN): unlike the
            # insecure opt-out, a CA-pin keeps the channel verified,
            # so it is the recommended state, not a footgun.
            logger.info(
                "connector_tls_ca_pinned",
                target=getattr(target, "name", None),
                host=getattr(target, "host", None),
                ca_pin_digest=_ca_pin_digest(ca_pin),
            )
            verify_arg = _build_ca_pinned_ssl_context(ca_pin)
        elif not verify_tls:
            # Per-target, audited last resort (evoila/meho#1774):
            # the dispatch forwards a Vault-resolved credential over
            # an unverified channel, so make it loud and queryable.
            # The audit row is written on target create/update
            # (T1 #1780); this WARN marks the actual insecure
            # dispatch construction.
            logger.warning(
                "connector_tls_verification_disabled",
                target=getattr(target, "name", None),
                host=getattr(target, "host", None),
            )
            verify_arg = _insecure_ssl_context()
        # Pin the connection to a guard-validated address at the
        # socket boundary. The
        # ``_http_client`` pre-check above screens the stored name
        # on every acquisition; this transport re-screens inside
        # ``connect_tcp`` and dials only an address from that same
        # resolution, so a resolver that changes its answer between
        # the screen and the connect cannot steer the socket at a
        # blocked destination the guard never saw. It preserves the
        # original hostname for TLS SNI + cert verification and the
        # ``Host:`` header (only the TCP target is rewritten). A
        # blocked answer surfaces as ``SsrfBlockedError`` via
        # ``_pin_target_addresses``, reaching the dispatcher's
        # ``ConnectError`` arm and staying out of the retry policy.
        # The screen gets the port the socket dials.
        transport = build_port_pinned_async_transport(_pin_target_addresses, verify=verify_arg)
        client_class = self._client_class or _SameOriginRedirectClient
        client = client_class(
            base_url=self._base_url(target),
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=5.0),
            # Same-origin-only redirect following (see
            # ``_SameOriginRedirectClient``). Vendor REST surfaces
            # routinely canonicalise to a trailing slash with a
            # ``301`` (the govmomi/vcsim legacy ``/rest`` mount does
            # this; some real appliances do too behind a normalising
            # reverse proxy) — those benign single-origin hops are
            # still followed, so no vendor flow regresses. A
            # *cross-origin* ``3xx`` is refused: ``httpx``'s default
            # ``follow_redirects=True`` would replay the request off
            # origin and forward NSX's ``X-XSRF-TOKEN`` and the
            # login body to an attacker-controlled ``Location``
            # (open-redirect SSRF).
            transport=transport,
            # A refusal by the block list after this request says that a
            # part of the operation may have run.
            event_hooks={"request": [_note_request_sent]},
        )
        if isinstance(client, _ChangeRuleClient):
            client.change_rule = self.uses_change_rule()
        return client

    async def _ephemeral_http_client(self, target: Target) -> httpx.AsyncClient:
        """A new, un-pooled client for *target* -- same SSRF + TLS posture, own cookie jar.

        The caller owns it and must ``aclose()`` it. Used for a session that
        must not share the pooled client's cookie jar (evoila/meho#3890).
        """
        try:
            await assert_public_destination_async(str(target.host), self._dial_port(target))
        except TargetDestinationBlockedError as exc:
            raise SsrfBlockedError(str(exc)) from exc
        return self._build_http_client(target)

    async def mount_op_path(self, target: Target, path: str, operator: Operator) -> str:
        """Map an ingested-descriptor *path* onto the wire path for *target*.

        Dispatcher hook for ``source_kind='ingested'`` ops: the G0.7
        pipeline stores spec-relative descriptor paths, and some vendor
        APIs expose those under a mount prefix the spec omits (vCenter
        REST: ``/api`` on modern, ``/rest`` on legacy/vcsim). The
        default is identity — most ingested APIs are reachable at the
        descriptor path verbatim. Vendor connectors that need a mount
        (see ``VmwareRestConnector``) override this; the override is a
        separate seam from ``_request_json`` / ``_post_json`` so their
        tenacity ``@retry`` wrapper stays intact.

        ``operator`` is threaded so an override that has to establish a
        session to learn the live mount (vCenter's modern→legacy
        fallback) authenticates under the same operator identity the
        transport call will use, rather than a credential-less stand-in.
        """
        del target, operator
        return path

    def _dial_port(self, target: Target) -> int | None:
        """The port the client dials for *target*, or ``None`` when it cannot tell.

        Read from the connector's own :meth:`_base_url`, so a connector with
        another default scheme (or port) is checked on the port it dials.
        A base URL that cannot be built gives ``None``; the guard then
        screens the host as before and the usual error comes later.
        """
        try:
            return _effective_port(httpx.URL(self._base_url(target)))
        except (KeyError, ValueError, httpx.InvalidURL):
            return None

    def _base_url(self, target: Target) -> str:
        scheme = _effective_scheme(target)
        default_port = _DEFAULT_PORTS[scheme]
        port = f":{target.port}" if target.port and target.port != default_port else ""
        return f"{scheme}://{target.host}{port}"

    def _request_extensions(self, target: Target) -> dict[str, Any]:
        """Return per-request httpx ``extensions`` for *target* (or ``{}``).

        Decouples the TLS SNI / certificate-verification name from the
        URL-derived ``host`` (evoila/meho#2002). httpx derives the TCP
        connect address, the wire ``Host:`` header **and** the TLS SNI +
        cert-CN/SAN verification name all from the client's ``base_url``
        host (httpcore: ``server_hostname =
        request.extensions["sni_hostname"] or origin.host``). When a
        target sets ``tls_server_name`` we keep ``base_url=https://<host>``
        (so connect + ``Host:`` stay the operator-routed value -- the IP a
        cert-CN-pinning appliance accepts) and pass
        ``extensions={"sni_hostname": <name>}`` so the handshake offers the
        override as SNI and verifies the presented cert's CN/SAN against
        it. This lets an operator keep ``verify_tls=true`` (and optionally
        ``tls_ca_pin``) against an appliance that pins its cert to an FQDN
        but demands ``Host: <IP>``, instead of dropping to the insecure
        ``verify_tls=false`` to dodge the hostname mismatch.

        Returns an empty dict when the target sets no override, so the
        request is dispatched byte-identically to today (the SNI / verify
        name derives from ``base_url`` as before). Threading it at this
        shared :class:`HttpConnector` layer covers both request helpers
        and the profiled-dispatch path automatically.
        """
        server_name = getattr(target, "tls_server_name", None)
        if server_name:
            return {"sni_hostname": server_name}
        return {}

    async def auth_headers(self, target: Target, operator: Operator) -> dict[str, str]:
        """Return auth headers for the request.

        Vendor connectors MUST override per ``target.auth_model``. The
        full :class:`~meho_backplane.auth.operator.Operator` is threaded
        here (not just ``operator.raw_jwt``) so a connector's credential
        loader can perform an operator-context Vault read via
        ``vault_client_for_operator(operator)`` — the locked decision in
        [docs/architecture/connector-auth.md](docs/architecture/connector-auth.md).
        ``Operator`` is frozen, so passing it down carries no
        confused-deputy risk.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must override auth_headers() — "
            f"target {target.name!r} uses {target.auth_model}"
        )

    def trace_response_body(self, response: httpx.Response) -> Any:
        """Return the response body the flight recorder records, or ``None``.

        ``None`` (the default) records the raw response bytes, redacted by
        the shared engine (header allowlist, credential-shape scrub, secret
        op families). A connector whose vendor answers can carry a secret in
        a field the shared engine does not know by shape -- for example a
        stored pairing password in a list of site pairings -- parses the
        body here and masks those fields, so the secret never reaches the
        stored trace. The returned value is recorded in place of the raw
        bytes and still passes the shared redaction. Must not raise.
        """
        del response
        return None

    def _trace_response_body(self, start: Any, response: httpx.Response) -> Any:
        """Call :meth:`trace_response_body` only while a trace is being captured."""
        if start is None:
            return None
        return self.trace_response_body(response)

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=2.0),
        retry=retry_if_exception(_retryable),
        reraise=True,
    )
    async def _request_json(
        self,
        target: Target,
        method: str,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Retryable JSON request. Only idempotent verbs (GET, HEAD, OPTIONS).

        Raises :exc:`ValueError` for non-idempotent verbs so a caller that
        accidentally passes ``POST``/``PATCH`` never gets silent retry of a
        side-effecting operation. Non-idempotent callers must use
        ``_post_json`` or call the httpx client directly. ``operator`` is
        forwarded to :meth:`auth_headers` so the connector can resolve
        credentials under the operator's identity. ``extra_headers`` are
        merged onto the connector-supplied :meth:`auth_headers` (e.g.
        header-located op params; per-call values win on a key clash).

        A ``204 No Content`` — or any empty-body — response returns ``{}``
        instead of raising from the JSON parse (#3082); see
        :func:`json_payload_or_empty`.
        """
        method = method.upper()
        if method not in _IDEMPOTENT_METHODS:
            raise ValueError(
                f"_request_json only accepts idempotent methods "
                f"{sorted(_IDEMPOTENT_METHODS)}; got {method!r}"
            )
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        if extra_headers:
            headers = {**headers, **extra_headers}
        # Flight-recorder vendor-call span (#3214). ``span_start`` is a single
        # contextvar read that returns ``None`` when capture is off, so an
        # un-recorded dispatch pays nothing; ``record_vendor_call`` is
        # best-effort (F7) and redacts headers/bodies at capture (#3213). The
        # span is recorded before ``raise_for_status`` so a vendor 4xx/5xx is
        # captured too. A transport fault (timeout, refused connection) has no
        # response, so it records an error span and re-raises (#3408). Each
        # retry attempt is its own vendor call and gets its own span.
        with flight_recorder_capture.request_body_values(json, path):
            _fr_start = flight_recorder_capture.span_start()
            trace_hops = _operation_request.set(True)
            try:
                resp = await _read_capped_json_response(
                    client,
                    method,
                    path,
                    params=params,
                    json=json,
                    headers=headers,
                    extensions=self._request_extensions(target),
                )
            except httpx.TransportError as exc:
                flight_recorder_capture.record_vendor_call_error(
                    _fr_start, method=_failed_method(exc, method), exc=exc
                )
                raise
            finally:
                _operation_request.reset(trace_hops)
            # After a followed redirect the last request can have another method
            # (an OPTIONS answered with a 303 is followed by a GET).
            flight_recorder_capture.record_vendor_call(
                _fr_start,
                method=resp.request.method,
                request_headers=headers,
                response=resp,
                request_body=json if resp.request.method == method else None,
                request_content_type="application/json" if json is not None else None,
                response_body=self._trace_response_body(_fr_start, resp),
            )
        resp.raise_for_status()
        return json_payload_or_empty(resp)

    async def _get_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Retried GET returning parsed JSON."""
        return await self._request_json(target, "GET", path, operator=operator, params=params)

    async def _post_json(
        self,
        target: Target,
        path: str,
        *,
        operator: Operator,
        verb: str = "POST",
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout: Any = httpx.USE_CLIENT_DEFAULT,
    ) -> dict[str, Any]:
        """Non-retried request for a non-idempotent verb, returning parsed JSON.

        Despite the name, this seam honours the *actual* non-idempotent
        verb passed in ``verb`` (``POST`` / ``PUT`` / ``PATCH`` /
        ``DELETE``) rather than hardcoding ``POST`` -- an ingested
        ``PUT``/``PATCH``/``DELETE`` op must reach the wire with its
        declared method, not be silently downgraded to a ``POST``. The
        verb is validated against :data:`_IDEMPOTENT_METHODS` so a caller
        that accidentally routes an idempotent verb here (which would skip
        the tenacity retry on :meth:`_request_json`) fails loudly.

        ``params`` are appended to the URL as the query string, mirroring
        :meth:`_request_json`'s ``params=`` seam so a non-idempotent
        ingested op whose spec locates a parameter ``in: query`` (not baked
        into the path literal) reaches the wire with its query string
        instead of silently dropping it (#3092). It defaults to ``None`` --
        httpx's own default for ``params=`` -- so every existing caller that
        omits it dispatches byte-identically to before.

        Two body shapes are supported and are mutually exclusive: ``json``
        serialises a JSON request body (``application/json``); ``data``
        serialises a form-encoded body (``application/x-www-form-urlencoded``
        -- the shape OAuth2 token grants and session-login POSTs require).

        ``extra_headers`` are merged onto the connector-supplied
        :meth:`auth_headers` (header-located op params; the per-call values
        win on a key clash). Retry on non-idempotent verbs is the caller's
        responsibility -- this seam deliberately stays outside the
        :meth:`_request_json` tenacity wrapper.

        ``timeout`` overrides the pooled client's default timeout for this
        one request only. It defaults to :data:`httpx.USE_CLIENT_DEFAULT`,
        so every caller that omits it keeps the connector's 30s client
        default byte-identically (httpx merges the sentinel to the client
        timeout in ``build_request``). A caller passes an explicit
        :class:`httpx.Timeout` only for a *synchronous* long-running POST
        whose body transfer legitimately outlasts 30s -- the vCenter
        library-item deploys, whose connection is held open for the whole
        multi-GB disk copy (#3076). The global client default is left
        untouched so ordinary reads/writes still fail fast on a dead target.

        A ``204 No Content`` — or any empty-body — response returns ``{}``
        instead of raising from the JSON parse (#3082): vCenter power
        actions and VM deletes acknowledge success with no body, and a
        parse failure here surfaced *after* the write took effect at the
        vendor. See :func:`json_payload_or_empty`.
        """
        verb = verb.upper()
        if verb in _IDEMPOTENT_METHODS:
            raise ValueError(
                f"_post_json only accepts non-idempotent methods; got {verb!r} "
                f"(idempotent verbs {sorted(_IDEMPOTENT_METHODS)} must use _request_json)"
            )
        if json is not None and data is not None:
            raise ValueError("_post_json accepts json= or data=, not both")
        client = await self._http_client(target)
        headers = await self.auth_headers(target, operator)
        if extra_headers:
            headers = {**headers, **extra_headers}
        # Flight-recorder vendor-call span (#3214) -- see ``_request_json``.
        # The request body is whichever of ``json`` / ``data`` was sent, with
        # its matching content-type; a hard-excluded (login / token / DELETE)
        # op has its body dropped by the redaction engine's family rules.
        with flight_recorder_capture.request_body_values(json if json is not None else data, path):
            _fr_start = flight_recorder_capture.span_start()
            trace_hops = _operation_request.set(True)
            try:
                resp = await _read_capped_json_response(
                    client,
                    verb,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    headers=headers,
                    extensions=self._request_extensions(target),
                    timeout=timeout,
                )
            except httpx.TransportError as exc:
                flight_recorder_capture.record_vendor_call_error(
                    _fr_start, method=_failed_method(exc, verb), exc=exc
                )
                raise
            finally:
                _operation_request.reset(trace_hops)
            flight_recorder_capture.record_vendor_call(
                _fr_start,
                method=resp.request.method,
                request_headers=headers,
                response=resp,
                request_body=json if json is not None else data,
                request_content_type=_post_body_content_type(json, data),
                response_body=self._trace_response_body(_fr_start, resp),
            )
        resp.raise_for_status()
        return json_payload_or_empty(resp)

    async def _send_traced(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Send one operation request through *client* and put it into the trace.

        For a typed connector that sends its own request instead of using
        :meth:`_post_json`. The trace gets one entry for each request that
        was sent, with its own method: each followed redirect and the last
        answer. Call it only for the operation's own request, never for a
        login, so login traffic stays out of the trace. The answer is
        returned as it is; the caller checks its status.
        """
        body = json if json is not None else data
        with flight_recorder_capture.request_body_values(body, path):
            _fr_start = flight_recorder_capture.span_start()
            trace_hops = _operation_request.set(True)
            try:
                resp = await client.request(
                    method,
                    path,
                    json=json,
                    data=data,
                    params=params,
                    headers=headers,
                    extensions=extensions,
                )
            except httpx.TransportError as exc:
                flight_recorder_capture.record_vendor_call_error(
                    _fr_start, method=_failed_method(exc, method), exc=exc
                )
                raise
            finally:
                _operation_request.reset(trace_hops)
            flight_recorder_capture.record_vendor_call(
                _fr_start,
                method=resp.request.method,
                request_headers=headers,
                response=resp,
                request_body=body if resp.request.method == method else None,
                request_content_type=_post_body_content_type(json, data),
                response_body=self._trace_response_body(_fr_start, resp),
            )
        return resp

    async def aclose(self) -> None:
        """Close all pooled clients. Called by lifespan or per-target cleanup."""
        async with self._lock:
            for client in self._clients.values():
                await client.aclose()
            self._clients.clear()
