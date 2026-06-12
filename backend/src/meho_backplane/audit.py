# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Synchronous audit-write middleware.

For every authenticated HTTP request the middleware writes one row
into the ``audit_log`` table **before** yielding the response back
to the ASGI send chain. The semantics are deliberately fail-closed:
if the audit insert fails, the request fails with HTTP 500 and the
operator never sees a successful response. An unaudited action is
an unallowed action.

Why pure ASGI rather than ``starlette.middleware.base.BaseHTTPMiddleware``
======================================================================

The chassis-stage :class:`~meho_backplane.middleware.RequestContextMiddleware`
is pure ASGI for a load-bearing reason that applies here too:
``BaseHTTPMiddleware.dispatch`` runs the wrapped app inside an
``anyio.create_task_group`` / ``task_group.start_soon`` pair, which
means the inner app receives a *copy* of the outer task's
``contextvars.Context``. Any contextvars bound inside the handler —
including the ``operator_sub`` that
:func:`~meho_backplane.middleware.verify_jwt_and_bind` writes —
disappear when the dispatch task resumes after ``await call_next(...)``.
Empirically (verified against starlette 1.0.0 + python 3.12), the
``BaseHTTPMiddleware`` shape sees an empty contextvars dict on the
post-handler side; the pure-ASGI shape sees the binding intact.

Reading ``operator_sub`` from contextvars is the *only* way to attribute
the audit row to the operator without re-reading the JWT — the
middleware runs after the route, so the response status is observable;
the JWT has already been validated by ``verify_jwt`` and the result
encoded into a contextvar by ``verify_jwt_and_bind``. Re-validating
the JWT here would double the JWKS round-trip and risk drift between
"who the auth layer authorised" and "who the audit row attributes".
The same wrapper binds ``tenant_id`` (as the canonical UUID string),
which the middleware re-parses into a :class:`uuid.UUID` before
writing the new ``audit_log.tenant_id`` column (see :func:`_resolve_tenant_id`).

Skip rules
==========

* **Unauthenticated requests** — no ``operator_sub`` in contextvars.
  Public surfaces (``/healthz``, ``/version``, ``/ready``,
  ``/metrics``, the unauthenticated ``/`` identity route, and any
  401 response from a protected route that failed JWT validation
  before the binding fired) are never audited; there is no operator
  to attribute. The skip rule keys on the contextvar's presence
  rather than path-matching the public surfaces explicitly so the
  rule cannot drift if a new public path is added.

Tenant-id binding contract
==========================

Every authenticated request that reaches the audit branch MUST have
``tenant_id`` bound in contextvars (``verify_jwt_and_bind`` binds it
unconditionally on top of ``operator_sub``). A missing or malformed
``tenant_id`` in this slot is a programming bug, not a runtime
condition: the audit row is still committed (with ``tenant_id=None``,
which T1's nullable column allows) but ``audit_missing_tenant_id`` /
``audit_malformed_tenant_id`` is logged at error level so on-call sees
the invariant violation. Failing the request hard would compound a
programming bug into a 500 with no audit trace; emitting a loud log
plus the partially-attributed row is the better tradeoff. See
:func:`_resolve_tenant_id` for the mechanism.

Fail-closed buffering
=====================

Writing the audit row before the response yields requires the
middleware to *buffer* the inner app's ASGI send messages and decide
whether to forward them or replace them with a 500. The buffer holds
the ``http.response.start`` + ``http.response.body`` messages until
the audit insert completes. On audit success the buffered messages
are forwarded verbatim. On audit failure the buffer is discarded and
a fresh 500 ``{"detail": "audit_write_failed"}`` response is sent.

The buffer is bounded by the response itself (typical FastAPI JSON
responses are kilobytes). Streaming routes are the deliberate
exception: a ``text/event-stream`` response (the SSE feeds at
``/api/v1/feed`` and ``/ui/broadcast/stream``) is **not** buffered —
buffering an open-ended SSE generator until the inner app returns
delivers zero bytes to the client for the life of the connection
(#1389). Such responses are forwarded chunk-by-chunk as the generator
yields, and the audit row is written when the stream ends (normal
completion or a client-disconnect ``CancelledError``). The fail-closed
500 swap cannot apply once a stream's ``http.response.start`` is on the
wire; the audit-write failure is still logged loudly, and the SSE
handler surfaces transport faults to the subscriber as an
``event: feed_error`` frame. Non-streaming JSON routes keep the
buffered fail-closed-500 contract verbatim.

References
----------
* https://www.starlette.io/middleware/ — pure-ASGI vs BaseHTTPMiddleware
* https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html — async session lifecycle
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.broadcast import (
    BroadcastEvent,
    compute_effective_broadcast_detail,
    publish_event,
    read_request_override,
    redact_payload,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog
from meho_backplane.operations._audit import work_ref_var

__all__ = ["AuditMiddleware", "bind_preallocated_audit_id"]

#: Body returned when the audit insert fails (fail-closed 500). Pinned
#: as a module constant so the contract is greppable from tests.
_AUDIT_FAILURE_BODY: Final[bytes] = json.dumps({"detail": "audit_write_failed"}).encode("utf-8")

_RESPONSE_START: Final[str] = "http.response.start"
_RESPONSE_BODY: Final[str] = "http.response.body"

#: Content-type the middleware treats as a live stream that must NOT be
#: buffered. Matched as a prefix against the lowercased ``content-type``
#: header value on ``http.response.start`` so the ``; charset=utf-8``
#: suffix Starlette's :class:`~starlette.responses.StreamingResponse`
#: appends still matches. Both SSE surfaces — ``GET /api/v1/feed``
#: (G6.1-T4) and ``GET /ui/broadcast/stream`` (G10.1) — set this media
#: type; an open-ended SSE generator never completes, so buffering its
#: send messages until the inner app returns delivers zero bytes to the
#: client for the life of the connection (#1389).
_EVENT_STREAM_MEDIA_TYPE: Final[bytes] = b"text/event-stream"


def _is_event_stream_start(message: Message) -> bool:
    """Return ``True`` when *message* is an ``http.response.start`` for an SSE body.

    Inspects the ``content-type`` header on the start message. ASGI
    header names are byte strings and case-insensitive per RFC 9110;
    the value carries the ``; charset=...`` suffix Starlette appends,
    so the comparison is a lowercased prefix match against
    :data:`_EVENT_STREAM_MEDIA_TYPE`.
    """
    if message.get("type") != _RESPONSE_START:
        return False
    headers: list[tuple[bytes, bytes]] = message.get("headers") or []
    for name, value in headers:
        if name.lower() == b"content-type":
            return bool(value.lower().startswith(_EVENT_STREAM_MEDIA_TYPE))
    return False


def _coerce_request_id(raw: object) -> uuid.UUID | None:
    """Best-effort UUID parse for the request_id contextvar value.

    The :class:`~meho_backplane.middleware.RequestContextMiddleware`
    binds the request_id either from an incoming ``X-Request-Id``
    header (operator-controlled) or from ``uuid4().hex`` (mints a
    valid UUID). Operators using opaque hex strings, k8s request ids,
    or anything else that does not parse as a UUID still get an audit
    row — just with ``request_id = NULL``. Failing the audit insert
    on a request-shape mismatch would convert a benign client into a
    5xx; that is the wrong tradeoff for v0.1.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        return None


#: Prefix routes use to namespace contextvar keys that should land in
#: the ``audit_log.payload`` JSON. A route binds e.g.
#: ``structlog.contextvars.bind_contextvars(audit_query_hash="…")``
#: before yielding the response; :func:`_resolve_audit_payload` reads
#: every ``audit_*`` contextvar at audit-write time, strips the
#: prefix, and merges the result into the payload dict. Routes that
#: bind nothing get the empty-payload behaviour today's audit_log
#: rows carry -- no opt-in cost for the chassis-era surfaces.
#:
#: The prefix discipline is what keeps the contextvar namespace from
#: colliding with the load-bearing ``operator_sub`` / ``tenant_id`` /
#: ``request_id`` keys that the audit middleware itself reads, and
#: lets a grep for ``audit_`` surface every route that enriches the
#: payload.
_AUDIT_PAYLOAD_PREFIX: Final[str] = "audit_"

#: Contextvar key a route can bind to pre-allocate the audit row's
#: ``id`` before the AuditMiddleware allocates a fresh ``uuid4()``.
#:
#: G7.1-T2 (#314): the convention CRUD routes need to write a
#: ``tenant_convention_history`` row whose ``audit_id`` soft-FK
#: points at the audit row that the middleware writes for the same
#: request. The middleware runs *after* the route handler returns,
#: so the row's id is otherwise unavailable to the handler -- which
#: would force a second audit row written by the handler (the
#: topology-nodes pattern), producing two audit rows per convention
#: write. Pre-allocating the id in the handler and binding it here
#: lets the middleware reuse it; the handler stores the same uuid
#: on the history row inside its own transaction.
#:
#: Deliberately NOT under :data:`_AUDIT_PAYLOAD_PREFIX` so the value
#: does not also land in the audit row's payload dict (the id is the
#: row's primary key, not a payload field). The contextvar is opt-in;
#: when unset the middleware falls back to allocating a fresh uuid4
#: as before -- no behavioural change for the chassis-era surfaces.
_PREALLOCATED_AUDIT_ID_KEY: Final[str] = "preallocated_audit_id"


def _resolve_audit_payload() -> dict[str, Any]:
    """Build the audit payload from ``audit_*`` contextvars.

    Reads every key in the current structlog contextvar context whose
    name starts with :data:`_AUDIT_PAYLOAD_PREFIX`, strips the prefix,
    and returns the result as a fresh dict. ``None`` values are
    dropped so a route can ``bind_contextvars(audit_kind=None)``
    without writing a ``"kind": null`` entry. Empty dict when no
    routes bound anything (the chassis-era default).
    """
    contextvars = structlog.contextvars.get_contextvars()
    payload: dict[str, Any] = {}
    for key, value in contextvars.items():
        if not key.startswith(_AUDIT_PAYLOAD_PREFIX):
            continue
        if value is None:
            continue
        stripped = key[len(_AUDIT_PAYLOAD_PREFIX) :]
        if stripped:
            payload[stripped] = value
    return payload


def bind_preallocated_audit_id(audit_id: uuid.UUID) -> None:
    """Pre-allocate the audit row's primary key for the current request.

    The AuditMiddleware otherwise generates a fresh ``uuid4()`` per
    request when it commits the audit row (after the handler
    returns). Routes that need the audit row's id *before* commit --
    typically to write a soft-FK from an in-transaction history /
    side-channel row -- can call this helper inside the handler with
    a uuid they minted themselves; the middleware then commits the
    audit row with that same id instead of allocating its own.

    G7.1-T2 (#314) ships the convention CRUD routes which write a
    ``tenant_convention_history`` row with an ``audit_id`` soft-FK
    in the same DB transaction as the convention mutation. Without
    this primitive the route would have to either (a) skip the
    chassis audit and write its own audit row inline (producing two
    audit rows per write because the middleware also fires), or
    (b) reconcile a NULL ``audit_id`` after-the-fact (forensic
    fragility). Pre-allocating the id keeps the contract one-row,
    one-write, one-soft-FK.

    The helper is intentionally side-effect-only: it binds the uuid
    onto the structlog contextvar slot the middleware reads. Routes
    pass the same uuid into their own
    :class:`~meho_backplane.db.models.AuditLog` writes (if any) --
    not relevant for convention CRUD (the middleware is the sole
    audit writer there) but documented here for completeness.

    Opt-in: when no route calls this helper the middleware allocates
    a fresh uuid4 (the v0.1 chassis behaviour). No behavioural
    change for the chassis-era surfaces.
    """
    structlog.contextvars.bind_contextvars(
        **{_PREALLOCATED_AUDIT_ID_KEY: str(audit_id)},
    )


def _resolve_preallocated_audit_id() -> uuid.UUID | None:
    """Pull the pre-allocated audit_id out of the contextvar, if any.

    Returns the uuid bound by :func:`bind_preallocated_audit_id`
    in the active request, or ``None`` when the contextvar is unset
    or holds an unparseable value. A bound value that fails the
    UUID parse logs a warning and falls back to ``None`` -- the
    middleware then mints a fresh uuid4 the way it did before T2,
    so a programming bug in a route never blocks the audit row from
    being written.
    """
    raw = structlog.contextvars.get_contextvars().get(_PREALLOCATED_AUDIT_ID_KEY)
    if raw is None:
        return None
    if not isinstance(raw, str):
        structlog.get_logger(__name__).warning(
            "audit_preallocated_id_malformed",
            value=repr(raw),
        )
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        structlog.get_logger(__name__).warning(
            "audit_preallocated_id_malformed",
            value=raw,
        )
        return None


def _resolve_target_id() -> uuid.UUID | None:
    """Pull ``target_id`` out of contextvars and parse it as :class:`uuid.UUID`.

    :func:`~meho_backplane.targets.resolver.resolve_target` binds
    ``target_id`` as ``str(target.id)`` at its single exit point
    (G0.3-T4). Requests that never call ``resolve_target`` (list,
    create, and all non-target routes) have ``target_id=None`` from
    :func:`~meho_backplane.middleware.verify_jwt_and_bind`'s slot
    initialisation; those land as ``None`` here, which is correct.

    A bound value that fails UUID parse indicates a programming error
    (the resolver only binds the ORM-generated UUID, which is always
    valid). The row is still committed with ``target_id=None`` and the
    malformed value is logged so the invariant violation is visible.
    """
    raw = structlog.contextvars.get_contextvars().get("target_id")
    if raw is None:
        return None
    log = structlog.get_logger()
    if not isinstance(raw, str):
        log.error("audit_malformed_target_id", value=raw)
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        log.error("audit_malformed_target_id", value=raw)
        return None


async def _write_audit_row(
    *,
    audit_id: uuid.UUID,
    operator_sub: str,
    tenant_id: uuid.UUID | None,
    target_id: uuid.UUID | None,
    method: str,
    path: str,
    status_code: int,
    request_id: uuid.UUID | None,
    duration_ms: float,
    payload: dict[str, Any],
) -> None:
    """Open a session and commit one ``AuditLog`` row.

    Lifted out of :meth:`AuditMiddleware.__call__` so the call
    site stays focused on response-buffering control flow. Exceptions
    propagate to the caller, which converts them into the fail-closed
    500 path.

    The *payload* dict is **caller-resolved** as of G6.3-T2 (#379) -- the
    caller walks ``_resolve_audit_payload`` first, then optionally
    augments the result with ``broadcast_detail_origin`` from the
    G6.3 resolver before handing it in. Centralising the payload walk
    inside this helper (the pre-T2 shape) made the broadcast resolver
    unable to inject its decision-origin into the audit row, because
    the row had already committed by the time the publish hook ran.

    ``audit_id`` is pre-generated by the caller so the publish-on-write
    hook references the same id on the
    :class:`~meho_backplane.broadcast.events.BroadcastEvent` it emits
    after the audit commit succeeds.
    """
    sessionmaker = get_sessionmaker()
    # work_ref I1-T1 #1655 -- read the external change-ticket reference
    # off the contextvar. The ``work_ref`` column is added by migration
    # ``0038``; guard on the attribute so the writer stays
    # forward-compatible if the model predates the migration (the same
    # discipline the dispatcher writer uses for the runbook columns).
    # ``None`` until the bind source (I1-T2) lands.
    soft_fk_kwargs: dict[str, Any] = {}
    if hasattr(AuditLog, "work_ref"):
        soft_fk_kwargs["work_ref"] = work_ref_var.get()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
            actor_sub=resolve_actor_sub(),
            tenant_id=tenant_id,
            target_id=target_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            duration_ms=Decimal(str(duration_ms)),
            payload=payload,
            **soft_fk_kwargs,
        )
        session.add(row)
        await session.commit()


def _classify_http_status(status_code: int, handler_exc: BaseException | None) -> str:
    """Map an HTTP status + handler-exc state to the broadcast result-status trichotomy.

    The ``BroadcastEvent.result_status`` field is one of ``"ok"`` /
    ``"error"`` / ``"denied"``. ``403`` and ``401`` (the access-denial
    statuses the chassis emits) map to ``"denied"``; every other 4xx
    + 5xx + a synthetic 500 from a handler exception map to
    ``"error"``; everything else (2xx / 3xx, no exception) is
    ``"ok"``. The chassis emits 4xx for authz failures, 5xx for
    server faults; splitting them into the two non-ok buckets lets
    feed subscribers tell "this operator hit a wall" (denied) from
    "this op blew up" (error) without parsing the status code on
    every event.
    """
    if handler_exc is not None:
        return "error"
    if status_code in (401, 403):
        return "denied"
    if status_code >= 400:
        return "error"
    return "ok"


def _resolve_op_id_and_class_override(
    *,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> tuple[str, str | None]:
    """Pull route-bound op_id / op_class overrides out of *payload*.

    The op_id heuristic for chassis HTTP routes is
    ``f"http.{method.lower()}:{path}"`` -- the ``:`` separator avoids
    accidental matches against
    :data:`~meho_backplane.broadcast.events._READ_SUFFIXES` /
    :data:`~meho_backplane.broadcast.events._WRITE_SUFFIXES` (a route
    ending in ``.list`` would otherwise classify as ``read``).

    A route may publish under a connector-style ``op_id`` by binding
    ``audit_op_id`` / ``audit_op_class`` contextvars before yielding
    -- :func:`_resolve_audit_payload` strips the ``audit_`` prefix so
    they land in *payload* as ``op_id`` / ``op_class``. The G4.3
    retrieval-usage route at ``GET /api/v1/retrieve/usage`` is the
    current consumer: ``meho.retrieval.usage`` has no recognisable
    verb suffix, so without the explicit ``op_class="audit_query"``
    override the broadcast would emit the full request payload and
    defeat the aggregate-only-for-audit-query discipline in decision
    #3 of ``docs/planning/v0.2-decisions.md``.

    Returns ``(op_id, op_class_override_or_None)``. The op_class
    override is threaded into :func:`compute_effective_broadcast_detail`
    so the resolver does not re-run :func:`classify_op` against a
    non-conforming op_id.
    """
    op_id_override = payload.get("op_id")
    op_id = (
        op_id_override
        if isinstance(op_id_override, str) and op_id_override
        else f"http.{method.lower()}:{path}"
    )
    op_class_override = payload.get("op_class")
    op_class = (
        op_class_override if isinstance(op_class_override, str) and op_class_override else None
    )
    return op_id, op_class


async def _publish_broadcast_event(
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    operator_sub: str,
    op_id: str,
    op_class: str,
    detail: Literal["full", "aggregate"],
    status_code: int,
    payload: dict[str, Any],
    handler_exc: BaseException | None,
) -> None:
    """Build the per-request :class:`BroadcastEvent` and publish it.

    As of G6.3-T2 (#379) the *(op_class, detail)* pair is resolved
    upstream by :func:`compute_effective_broadcast_detail` -- this
    helper no longer calls :func:`classify_op` or decides the redaction
    branch on its own. The split lets the resolver inject its
    decision-origin into the audit row's payload *before* the row
    commits (see :class:`AuditMiddleware`); this helper renders the
    decided detail into the wire payload and publishes.

    ``principal_name`` / ``target_name`` are ``None`` for HTTP-path
    events -- the JWT name claim isn't bound to contextvars by
    :func:`~meho_backplane.middleware.verify_jwt_and_bind` (only
    ``operator_sub`` and ``tenant_id`` are), and the connector
    ``target_name`` doesn't apply to chassis routes. T5 (CLI watch) +
    T6 (MCP resource) read events back from the stream and JOIN
    against ``audit_log`` by ``audit_id`` for any enrichment a
    downstream consumer needs.
    """
    result_status = _classify_http_status(status_code, handler_exc)
    event = BroadcastEvent(
        event_id=uuid.uuid4(),
        ts=datetime.now(UTC),
        tenant_id=tenant_id,
        principal_sub=operator_sub,
        op_id=op_id,
        op_class=op_class,
        result_status=result_status,
        audit_id=audit_id,
        payload=redact_payload(op_class, payload, result_status, detail=detail),
    )
    await publish_event(event)


class _InnerAppResult:
    """Mutable view of the inner app's response, observable on exception.

    ``status_code`` and ``streamed`` are written as the inner app emits
    its first ``http.response.start`` message, so the audit-write code
    can read them even when the inner app later raises
    :class:`asyncio.CancelledError` (an SSE client disconnect, which
    must still produce an audit row for the closed session — #1389).
    ``handler_exc`` holds a non-cancellation :class:`Exception` the
    inner app raised, mapped to the fail-closed 500 path.
    """

    __slots__ = ("handler_exc", "status_code", "streamed")

    def __init__(self) -> None:
        self.status_code: int = 0
        self.handler_exc: BaseException | None = None
        #: True once an ``http.response.start`` for ``text/event-stream``
        #: was seen — its messages were forwarded live, not buffered.
        self.streamed: bool = False


async def _run_inner_app_buffered(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    send: Send,
    buffered: list[Message],
    result: _InnerAppResult,
) -> None:
    """Invoke the inner app, buffering its send messages — except SSE streams.

    Populates the caller-owned *result* in place. The default contract
    is unchanged: ``http.response.start`` + ``http.response.body``
    messages are appended to *buffered* and the caller forwards them only
    after the audit insert decides whether to keep them or replace them
    with a fail-closed 500.

    The exception is a ``text/event-stream`` response
    (:func:`_is_event_stream_start`): an open-ended SSE generator never
    completes, so buffering its messages until the inner app returns
    delivers zero bytes to the client for the life of the connection
    (#1389). Once the start message is recognised as a stream, that
    message and every subsequent body chunk are forwarded through *send*
    immediately, and :attr:`_InnerAppResult.streamed` is set so the
    caller skips the post-audit re-forward and the fail-closed-500
    replacement (the response headers are already on the wire — the SSE
    feed handler surfaces a transport failure as an ``event: feed_error``
    frame instead, see :mod:`meho_backplane.api.v1.feed`).

    When the inner app raises a non-cancellation :class:`Exception`,
    non-streamed buffered messages are cleared (a partially-emitted
    response cannot be safely forwarded) and the status is synthesised
    to 500 so the audit row reflects
    :class:`~starlette.middleware.errors.ServerErrorMiddleware`'s
    eventual verdict. ``CancelledError`` is intentionally *not* caught
    so client disconnects still cancel the task tree cleanly — and
    because *result* is caller-owned, it is fully populated by the time
    the cancellation propagates, so the caller's ``except`` arm still
    writes the audit row for a streamed session that ended on disconnect.
    """

    async def buffered_send(message: Message) -> None:
        if message["type"] == _RESPONSE_START:
            result.status_code = int(message.get("status", 0))
            if _is_event_stream_start(message):
                result.streamed = True
        if result.streamed:
            # SSE: forward live so each chunk reaches the client as the
            # generator yields it, rather than after the (never-ending)
            # stream closes.
            await send(message)
            return
        buffered.append(message)

    try:
        await app(scope, receive, buffered_send)
    except Exception as exc:
        if not result.streamed:
            buffered.clear()
            result.status_code = 500
        result.handler_exc = exc


def _resolve_request_metadata(
    scope: Scope,
) -> tuple[str, str, uuid.UUID | None]:
    """Pull ``method`` / ``path`` / ``request_id`` out of scope + contextvars.

    Centralised so :meth:`AuditMiddleware.__call__` does not need to
    inline three separate ``isinstance(...) else ""`` defences. The
    request_id is coerced from the ``request_id`` contextvar bound by
    :class:`~meho_backplane.middleware.RequestContextMiddleware`; opaque
    values land as ``None`` rather than failing the insert.
    """
    request_id = _coerce_request_id(
        structlog.contextvars.get_contextvars().get("request_id"),
    )
    method = scope.get("method", "")
    if not isinstance(method, str):
        method = ""
    path = scope.get("path", "")
    if not isinstance(path, str):
        path = ""
    return method, path, request_id


def _resolve_tenant_id(
    *,
    operator_sub: str,
    method: str,
    path: str,
    log: Any,
) -> uuid.UUID | None:
    """Pull ``tenant_id`` out of contextvars and parse it as :class:`uuid.UUID`.

    ``verify_jwt_and_bind`` binds ``tenant_id`` as ``str(operator.tenant_id)``
    immediately after :func:`~meho_backplane.auth.jwt.verify_jwt` validates
    the token. Reaching this helper from the audit middleware means the
    request has already cleared the auth dependency, so a missing or
    malformed value is a programming bug — not a runtime condition the
    operator caused. Both branches are surfaced loudly:

    * **Missing** — the auth dependency ran (``operator_sub`` is set) but
      no one bound ``tenant_id``. Logs ``audit_missing_tenant_id`` at
      error level. This trips when a future contributor stages a
      protected route through ``Depends(verify_jwt)`` directly instead
      of ``Depends(verify_jwt_and_bind)``, or when test code calls
      :func:`structlog.contextvars.clear_contextvars` mid-request.
    * **Malformed** — ``tenant_id`` is bound to a value that does not
      parse as a UUID. Logs ``audit_malformed_tenant_id`` with the bad
      value verbatim — that value originated from a JWT the trusted
      issuer signed, so it belongs to the issuer's claim namespace, not
      to a caller-controlled secret.

    Both failure modes return ``None``, which the migration in T1 leaves
    nullable on ``audit_log.tenant_id``. The audit row is still
    written — losing the row would compound a programming bug into a
    full request failure with no audit trace, exactly the wrong
    tradeoff. The error log is the operator's signal that the
    invariant was violated; the missing column value on the row is the
    durable artifact for postmortem.
    """
    raw = structlog.contextvars.get_contextvars().get("tenant_id")
    if raw is None:
        log.error(
            "audit_missing_tenant_id",
            operator_sub=operator_sub,
            method=method,
            path=path,
        )
        return None
    if not isinstance(raw, str):
        log.error(
            "audit_malformed_tenant_id",
            operator_sub=operator_sub,
            method=method,
            path=path,
            value=raw,
        )
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
        log.error(
            "audit_malformed_tenant_id",
            operator_sub=operator_sub,
            method=method,
            path=path,
            value=raw,
        )
        return None


async def _send_audit_failure_response(send: Send) -> None:
    """Emit the canonical fail-closed 500 ``{"detail": "audit_write_failed"}``.

    Hand-rolled so the audit middleware has zero dependency on
    FastAPI's exception handlers (which run *inside* the route, but
    the audit middleware is outside it on the response side). The
    body is a fixed module constant; no operator-controllable
    substrings can leak through.
    """
    await send(
        {
            "type": _RESPONSE_START,
            "status": 500,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_AUDIT_FAILURE_BODY)).encode("ascii")),
            ],
        },
    )
    await send(
        {
            "type": _RESPONSE_BODY,
            "body": _AUDIT_FAILURE_BODY,
        },
    )


#: Path prefixes the chassis audit middleware skips entirely. The MCP
#: transport at ``/mcp`` is JSON-RPC over POST and a single request can
#: carry multiple tool / resource invocations; the per-operation audit
#: row is written by :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`
#: from inside each handler (G0.5-T5, #250). A chassis-level row per
#: ``/mcp`` POST would be the wrong granularity for G8's audit queries.
_AUDIT_SKIP_PATH_PREFIXES: tuple[str, ...] = ("/mcp",)


class AuditMiddleware:
    """Pure-ASGI middleware that writes one audit row per authenticated request.

    Stateless beyond ``self.app`` — every request lives in its own
    closure scope. The ASGI contract requires a single middleware
    instance to handle concurrent requests; storing per-request data
    on ``self`` would race.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # G0.5-T5 (#250): MCP requests are audited at per-operation
        # granularity by the MCP handlers themselves, not at the
        # JSON-RPC-envelope layer. Skip the chassis audit path entirely
        # for ``/mcp`` so we don't double-attribute (or, worse,
        # under-attribute when the chassis row hides a multi-op POST).
        scope_path = scope.get("path", "")
        if isinstance(scope_path, str) and scope_path.startswith(
            _AUDIT_SKIP_PATH_PREFIXES,
        ):
            await self.app(scope, receive, send)
            return

        start = time.monotonic()

        # Buffer the inner app's send messages so we can decide, after
        # the audit insert, whether to forward them or replace with a
        # fresh 500. Single-shot JSON responses keep the buffer small.
        # The exception is a ``text/event-stream`` response: its messages
        # are forwarded live by :func:`_run_inner_app_buffered` (an
        # open-ended SSE generator never returns, so buffering would
        # deliver zero bytes — #1389). The inner-app invocation is
        # wrapped so a handler exception still produces an audit row for
        # the failed action.
        #
        # An SSE client disconnect raises :class:`asyncio.CancelledError`
        # out of the inner app; the audit row for the now-closed session
        # is still written (the ``result`` holder is fully populated by
        # then) before the cancellation propagates to unwind the task
        # tree per asyncio's contract. That disconnect-path write is
        # shielded (#1599) so a second cancellation mid-INSERT cannot
        # silently drop the row — see the ``except`` arm below.
        buffered: list[Message] = []
        result = _InnerAppResult()
        try:
            await _run_inner_app_buffered(
                self.app,
                scope,
                receive,
                send,
                buffered,
                result,
            )
        except asyncio.CancelledError as cancelled:
            # SSE client disconnect. ``result`` is caller-owned, so its
            # ``streamed`` / ``status_code`` reflect the headers already
            # sent on the first yield; write the audit row for the closed
            # session, then re-raise to unwind the task tree (Sonar
            # S7497 / asyncio cancellation contract).
            #
            # The write is run as a shielded task (#1599): a *second*
            # ``CancelledError`` delivered while ``_finalize`` is awaiting
            # the audit INSERT (task-tree teardown, server shutdown, an
            # enclosing ``timeout()`` or anyio cancel scope) would
            # otherwise propagate straight out of a bare ``await`` —
            # ``CancelledError`` is a ``BaseException``, so ``_finalize``'s
            # ``except Exception`` arm never sees it and the row is dropped
            # with no log line at all.
            #
            # ``ensure_future`` schedules the write on the loop and we keep
            # a strong reference (the loop holds only a weak one). We then
            # drain it to completion under ``asyncio.shield``. The drain is
            # a loop, not a single ``await``: cancellation can be delivered
            # repeatedly (an ASGI server re-cancelling the task on a
            # half-closed disconnect, an enclosing anyio cancel scope that
            # is *level-triggered* and re-raises at every checkpoint while
            # it stays cancelled), so a single ``await shield(write)`` may
            # be interrupted before the shielded task finishes. Re-awaiting
            # under a fresh shield each iteration lets the write run to
            # completion regardless of how many cancellations are
            # redelivered. The shielded task itself is never cancelled by
            # these (that is what ``shield`` guarantees); only our wait is.
            # Once the write is done we re-raise the original ``cancelled``
            # so the disconnect is honored, without suppressing it (no
            # ``Task.uncancel()``).
            write = asyncio.ensure_future(
                self._finalize(
                    scope=scope,
                    send=send,
                    buffered=buffered,
                    result=result,
                    duration_ms=round((time.monotonic() - start) * 1000, 2),
                ),
            )
            while not write.done():
                try:
                    await asyncio.shield(write)
                except asyncio.CancelledError:
                    # A redelivered cancellation interrupted the wait, not
                    # the shielded write. Loop to finish draining it; the
                    # original ``cancelled`` is re-raised below to honor the
                    # disconnect.
                    continue
            # Surface a ``_finalize`` failure (the handler itself raised);
            # ``write.result()`` re-raises it. An SSE disconnect never
            # populates ``handler_exc`` (the inner app only catches
            # ``Exception``), so a cleanly-closed stream's write returns
            # ``None`` here.
            write.result()
            raise cancelled

        duration_ms = round((time.monotonic() - start) * 1000, 2)
        await self._finalize(
            scope=scope,
            send=send,
            buffered=buffered,
            result=result,
            duration_ms=duration_ms,
        )

    # code-quality-allow: relocated legacy __call__ body (audit-write +
    # broadcast publish + forward control flow); #1389 only extracted it
    # so the normal-return and SSE-disconnect paths share one finalizer.
    async def _finalize(
        self,
        *,
        scope: Scope,
        send: Send,
        buffered: list[Message],
        result: _InnerAppResult,
        duration_ms: float,
    ) -> None:
        """Write the audit row and forward / replace / re-raise the response.

        Shared by the normal-return path and the SSE-disconnect
        (``CancelledError``) path. ``result.streamed`` selects between
        the buffered fail-closed-500 contract (non-streaming JSON routes,
        unchanged) and the already-streamed contract (SSE: bytes are on
        the wire, so the audit row is recorded but the response can no
        longer be replaced or re-forwarded — #1389).
        """
        status_code = result.status_code
        handler_exc = result.handler_exc
        operator_sub = structlog.contextvars.get_contextvars().get("operator_sub")

        if not isinstance(operator_sub, str) or not operator_sub:
            # No operator to attribute. Skip the audit write entirely
            # (public surfaces, 401s, and any other unauthenticated
            # path land here); forward the buffered response unchanged
            # — or, if the handler raised, re-raise so the outer
            # ServerErrorMiddleware builds the 500. Streamed responses
            # were already forwarded live, so there is nothing to flush.
            if handler_exc is not None and not result.streamed:
                raise handler_exc
            if not result.streamed:
                for message in buffered:
                    await send(message)
            return

        method, path, request_id = _resolve_request_metadata(scope)
        log = structlog.get_logger()
        tenant_id = _resolve_tenant_id(
            operator_sub=operator_sub,
            method=method,
            path=path,
            log=log,
        )
        # G7.1-T2 (#314): honor a pre-allocated audit_id when a route
        # bound one via :func:`bind_preallocated_audit_id`. The default
        # path (no binding) is unchanged -- fresh uuid4 per request.
        audit_id = _resolve_preallocated_audit_id() or uuid.uuid4()
        target_id = _resolve_target_id()

        # G6.3-T2 (#379): resolve broadcast detail BEFORE the audit row
        # commits so ``broadcast_detail_origin`` lands in the audit
        # payload. The resolver needs a tenant_id to consult its
        # per-tenant cache -- for malformed JWT-claim cases (tenant_id
        # is None) the audit row commits with no origin key and no
        # broadcast event fires.
        payload = _resolve_audit_payload()
        broadcast_op_id, broadcast_op_class_override = _resolve_op_id_and_class_override(
            method=method,
            path=path,
            payload=payload,
        )
        broadcast_decision: tuple[str, Literal["full", "aggregate"], str] | None = None
        # Snapshot of the resolver's raw_params view that the broadcast
        # event will render through :func:`redact_payload`. Kept distinct
        # from ``payload`` so the ``broadcast_detail_origin`` we inject
        # into the audit row below never reaches the broadcast feed --
        # the origin (especially ``tenant_rule:<uuid>``) is internal
        # audit-trail metadata that SSE / Slack / MCP-resource
        # subscribers must not see.
        broadcast_payload = payload
        if tenant_id is not None:
            broadcast_decision = await compute_effective_broadcast_detail(
                op_id=broadcast_op_id,
                tenant_id=tenant_id,
                raw_params=payload,
                request_override=read_request_override(),
                op_class_override=broadcast_op_class_override,
            )
            broadcast_payload = dict(payload)
            payload["broadcast_detail_origin"] = broadcast_decision[2]
            # G6.3-T3 (#380): record the resolver's effective detail
            # alongside the origin so ``meho audit query`` can answer
            # both "who/what decided" and "what detail did they get".
            # Audit-only -- the broadcast event renders ``detail`` via
            # ``redact_payload``'s shape; this key on the audit row is
            # for forensic queries, not for subscribers.
            payload["broadcast_detail_effective"] = broadcast_decision[1]

        try:
            await _write_audit_row(
                audit_id=audit_id,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                target_id=target_id,
                method=method,
                path=path,
                status_code=status_code,
                request_id=request_id,
                duration_ms=duration_ms,
                payload=payload,
            )
        except Exception:
            # Fail-closed: discard the buffered handler response and
            # send a fresh 500. The exception class is recorded on
            # the structured log line; we deliberately do *not* echo
            # the exception message into the response body or the log
            # message — a misconfigured DB driver could otherwise
            # leak DSN substrings into a 500 payload.
            log.exception(
                "audit_write_failed",
                method=method,
                path=path,
                status_code=status_code,
                duration_ms=duration_ms,
            )
            # Streamed (SSE) responses already sent ``http.response.start``
            # live, so the fail-closed 500 swap is impossible — the bytes
            # are on the wire. The audit-write failure is still logged
            # loudly above; the SSE handler surfaces transport faults to
            # the subscriber as an ``event: feed_error`` frame. Re-raise
            # only when the handler itself faulted (the task is already
            # unwinding); otherwise return so the live stream closes
            # cleanly rather than being torn down by a synthesised 500.
            if result.streamed:
                if handler_exc is not None:
                    raise handler_exc from None
                return
            # If the handler itself also raised, the audit-failure
            # response cannot be sent (the outer ServerErrorMiddleware
            # is about to take over); prefer surfacing the original
            # handler exception so the 500 carries the upstream
            # context rather than a misleading audit_write_failed.
            # ``from None`` suppresses the active audit-write exception
            # as the implicit ``__context__`` so the operator-facing
            # 500 doesn't conflate the two distinct failure modes.
            if handler_exc is not None:
                raise handler_exc from None
            await _send_audit_failure_response(send)
            return

        # Audit row was committed. The G6.1-T3 (#309) publish-on-write
        # hook fires here — exactly ONCE per audit commit, AFTER the
        # row landed but BEFORE the response forwards. Failure to
        # publish never bubbles up: ``publish_event`` is fail-open by
        # contract (Valkey unreachable, XADD-rejected, redis-py
        # teardown race all log + bump
        # ``broadcast_publish_errors_total`` and return silently). The
        # broadcast feed is the real-time view; the audit row is the
        # canonical record, so a missed event is a degraded broadcast,
        # not a failed operation. Skipped when ``tenant_id`` is None
        # (malformed JWT claim — the audit row still wrote with NULL
        # tenant_id but the broadcast event requires a UUID); also
        # skipped when the G6.3 resolver was not consulted for the
        # same reason (the two checks always agree).
        if tenant_id is not None and broadcast_decision is not None:
            broadcast_op_class, broadcast_detail, _origin = broadcast_decision
            await _publish_broadcast_event(
                audit_id=audit_id,
                tenant_id=tenant_id,
                operator_sub=operator_sub,
                op_id=broadcast_op_id,
                op_class=broadcast_op_class,
                detail=broadcast_detail,
                status_code=status_code,
                payload=broadcast_payload,
                handler_exc=handler_exc,
            )

        # If the handler raised, re-raise so the outer
        # ServerErrorMiddleware builds the canonical 500; the audit
        # row attributing the failed action is already persisted,
        # satisfying the "every authenticated action gets exactly one
        # row" contract for the 5xx path. An SSE client disconnect
        # surfaces as ``CancelledError`` re-raised by ``__call__`` after
        # this method returns — it is never recorded in ``handler_exc``
        # (``_run_inner_app_buffered`` only catches ``Exception``), so a
        # cleanly-closed stream falls through to the no-op return below.
        if handler_exc is not None:
            raise handler_exc

        # Audit committed and handler succeeded. A streamed response was
        # already forwarded chunk-by-chunk; only buffered (non-streaming)
        # responses are flushed here.
        if result.streamed:
            return
        for message in buffered:
            await send(message)
