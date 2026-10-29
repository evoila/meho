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

Wrapper row cutover (#3757)
===========================

With ``AUDIT_WRAPPER_ROWS_RETIRED=true`` this middleware no longer
writes a row for every request.
:func:`~meho_backplane.audit_cutover.wrapper_row_class` decides: routine
reads, dispatcher calls, validation failures and requests that already
wrote a classified row get no row (the request usage event records
them); a refused request gets a ``policy_decision`` row; any other change
gets an ``operation_execution`` row. With the setting off, every authenticated
request writes one ``legacy_request`` row, as described below.

Audit receipt header (#4976)
============================

A buffered response carries the id of the request's audit record in the
``Meho-Audit-Id`` header: the first ``operation_execution`` row the
request committed in the caller's name (the record of the change, for
example a connector edit or a dispatched operation). When there is no
such row, it is the first other classified row the request committed in
the caller's name (for example a decision row), else the wrapper row this
middleware wrote. A call allowed by a standing grant saves its approval
decision row first, so the header still names the execution row, the
same row as the ``audit_id`` in the response body. A request with no
record (a routine read after the cutover, an unauthenticated request, a
streamed response) gets no header. Routes that answer ``204 No Content``
have no body to carry the id, so ``meho`` reads it from this header and
prints ``audit receipt: <id>``.

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

from meho_backplane.audit_cutover import (
    classified_record_id,
    close_classified_record_scope,
    open_classified_record_scope,
    rest_request_is_routine,
    wrapper_row_class,
)
from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.broadcast import (
    compute_effective_broadcast_detail,
    read_request_override,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations._audit import work_ref_var
from meho_backplane.request_usage.collector import open_request_usage

__all__ = ["AuditMiddleware", "bind_preallocated_audit_id"]

#: Body returned when the audit insert fails (fail-closed 500). Pinned
#: as a module constant so the contract is greppable from tests.
_AUDIT_FAILURE_BODY: Final[bytes] = json.dumps({"detail": "audit_write_failed"}).encode("utf-8")

_RESPONSE_START: Final[str] = "http.response.start"
_RESPONSE_BODY: Final[str] = "http.response.body"

#: Lower-cased request-header name carrying the external change-ticket
#: reference for a REST, dispatch or MCP call (work_ref I1-T2 #1657;
#: MCP since #4256). Read off
#: the raw ASGI scope (the audit middleware is pure-ASGI and runs before
#: FastAPI route resolution, so there is no ``Request`` object to declare
#: a ``Header(...)`` param on -- same posture as ``X-Request-Id`` in
#: :func:`meho_backplane.middleware._extract_request_id`). Bound onto
#: :data:`~meho_backplane.operations._audit.work_ref_var` for the whole
#: request -- route handler and chassis audit write (#3804) -- so the
#: chassis ``audit_log.work_ref`` column and every service-level row
#: written inside the request carry it. Because
#: it is never declared as a FastAPI param, it does NOT appear in the
#: OpenAPI schema -- the documented surface (and thus the CLI snapshot)
#: is untouched by this header; only the ``CallOperationBody.work_ref``
#: field (a real body field) moves the documented schema.
_WORK_REF_HEADER: Final[bytes] = b"meho-work-ref"

#: Lower-cased response-header name carrying the id of the request's
#: audit record (#4976). See "Audit receipt header" in the module docstring.
_AUDIT_ID_HEADER: Final[bytes] = b"meho-audit-id"

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
    event_class: AuditEventClass,
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

    ``audit_id`` is pre-generated by the caller (a route may bind one
    with :func:`bind_preallocated_audit_id`).

    *event_class* comes from :func:`~meho_backplane.audit_cutover.wrapper_row_class`:
    ``legacy_request`` before the cutover (#3733), a decision or execution
    class after it (#3757).
    """
    sessionmaker = get_sessionmaker()
    # work_ref I1-T1 #1655 -- read the external change-ticket reference
    # off the contextvar. The ``work_ref`` column is added by migration
    # ``0039``; guard on the attribute so the writer stays
    # forward-compatible if the model predates the migration (the same
    # discipline the dispatcher writer uses for the runbook columns).
    # ``None`` until the bind source (I1-T2) lands.
    soft_fk_kwargs: dict[str, Any] = {}
    if hasattr(AuditLog, "work_ref"):
        soft_fk_kwargs["work_ref"] = work_ref_var.get()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            event_class=event_class,
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
    #3 of ``docs/decisions/locked-decisions.md``.

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


def _extract_work_ref(scope: Scope) -> str | None:
    """Return the inbound ``Meho-Work-Ref`` header value, or ``None``.

    The audit middleware reads the header straight off the raw ASGI
    ``scope`` (work_ref I1-T2 #1657) -- it is pure-ASGI and runs before
    route resolution, so there is no ``Request`` to hang a
    ``Header(...)`` param on, mirroring how
    :func:`meho_backplane.middleware._extract_request_id` reads
    ``X-Request-Id``. Header names are case-insensitive per RFC 9110
    §5.1, so the comparison lower-cases the wire name; the value is
    decoded ``latin-1`` (the byte range ASGI guarantees for header
    values) and stripped. An absent header, or a present-but-empty
    value, both return ``None`` so the audit row's ``work_ref`` lands
    NULL -- the correct quiet path for a call carrying no ticket.
    """
    for name, value in scope.get("headers", ()):
        if name.lower() == _WORK_REF_HEADER:
            decoded = value.decode("latin-1").strip()
            return decoded or None
    return None


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


def _add_audit_id_header(buffered: list[Message], audit_id: uuid.UUID | None) -> None:
    """Put ``Meho-Audit-Id: <audit_id>`` on the buffered response start message.

    Does nothing when *audit_id* is ``None`` or nothing was buffered (a
    handler that raised). The start message is replaced, not changed in
    place, so the inner app's header list is never touched.
    """
    if audit_id is None:
        return
    for index, message in enumerate(buffered):
        if message["type"] != _RESPONSE_START:
            continue
        headers = [
            (name, value)
            for name, value in message.get("headers") or []
            if name.lower() != _AUDIT_ID_HEADER
        ]
        headers.append((_AUDIT_ID_HEADER, str(audit_id).encode("ascii")))
        buffered[index] = {**message, "headers": headers}
        return


async def _forward_without_audit_row(
    send: Send,
    buffered: list[Message],
    result: _InnerAppResult,
) -> None:
    """Finish a request that gets no wrapper audit row.

    Forward the buffered response unchanged, or, if the handler raised,
    re-raise so the outer ServerErrorMiddleware builds the 500. Streamed
    responses were already forwarded live, so there is nothing to flush.
    """
    if result.streamed:
        return
    if result.handler_exc is not None:
        raise result.handler_exc
    for message in buffered:
        await send(message)


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

        # #3734: request usage boundary. This middleware is the outermost
        # place that sees every HTTP request (``/mcp`` included), so it owns
        # the one ``request_finished`` event. ``None`` when collection is off
        # or a boundary is already open (nested layer). The usage write runs
        # after the response is sent and never raises, so it cannot change
        # the audit write or the response.
        usage = open_request_usage()
        # #3757: watch for classified audit rows written inside the request,
        # so the wrapper row is not written twice (see audit_cutover).
        record_scope = open_classified_record_scope()
        try:
            if usage is None:
                await self._handle_http(scope, receive, send)
                return
            try:
                await self._handle_http(scope, receive, usage.wrap_send(send))
            finally:
                await usage.close(scope)
        finally:
            close_classified_record_scope(record_scope)

    async def _handle_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Audit one HTTP request: bind the work-ref, run the inner app, write the row."""
        start = time.monotonic()

        # work_ref I1-T2 (#1657), widened by #3804: bind the inbound
        # ``Meho-Work-Ref`` header onto :data:`work_ref_var` for the WHOLE
        # request -- the route handler and the chassis audit write in
        # ``_finalize``. Service-level audit writers that run inside the
        # handler (the connector review / ingest rows, a parked approval
        # request, the DISPATCH row when the body carries no ``work_ref``
        # of its own) read the var, so they inherit the request's change
        # ticket instead of landing NULL. A background task the handler
        # starts (the async ingest job) copies this context at creation,
        # so its rows carry it too. Token + ``try/finally`` / ``reset``:
        # the middleware is a single instance serving concurrent requests
        # on one event loop, so a leaked binding would mis-stamp the NEXT
        # request's rows. ``None`` header -> no bind -> NULL, unchanged.
        # The bind runs BEFORE the ``/mcp`` skip below (#4256), so the MCP
        # audit rows and the dispatch rows written inside each tool call
        # carry the header too. A ``work_ref`` tool argument still wins:
        # ``call_operation`` binds it over this value for its dispatch.
        work_ref_token = None
        work_ref_header = _extract_work_ref(scope)
        if work_ref_header is not None:
            work_ref_token = work_ref_var.set(work_ref_header)
        try:
            # G0.5-T5 (#250): MCP requests are audited at per-operation
            # granularity by the MCP handlers themselves, not at the
            # JSON-RPC-envelope layer. Skip the chassis audit row for
            # ``/mcp`` so we don't double-attribute (or, worse,
            # under-attribute when the chassis row hides a multi-op POST).
            scope_path = scope.get("path", "")
            if isinstance(scope_path, str) and scope_path.startswith(
                _AUDIT_SKIP_PATH_PREFIXES,
            ):
                await self.app(scope, receive, send)
                return
            await self._run_and_finalize(scope, receive, send, start)
        finally:
            if work_ref_token is not None:
                work_ref_var.reset(work_ref_token)

    # code-quality-allow: the pre-#3734 ``__call__`` body lives here
    # unchanged (moved out of ``_handle_http`` so the work-ref bind can
    # bracket it); splitting the shielded-cancellation drain is out of scope.
    async def _run_and_finalize(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        start: float,
    ) -> None:
        """Run the inner app, then write the audit row and forward the response."""
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
            # path land here).
            await _forward_without_audit_row(send, buffered, result)
            return

        method, path, request_id = _resolve_request_metadata(scope)
        # #3757: after the cutover, routine requests and requests that
        # already have a classified row get no wrapper row.
        route_template = getattr(scope.get("route"), "path_format", None)
        event_class = wrapper_row_class(
            status_code=status_code,
            routine=rest_request_is_routine(
                method,
                route_template if isinstance(route_template, str) else None,
            ),
            operator_sub=operator_sub,
        )
        if event_class is None:
            _add_audit_id_header(buffered, classified_record_id(operator_sub))
            await _forward_without_audit_row(send, buffered, result)
            return
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
        # is None) the audit row commits with no origin key. No
        # Broadcast event is published (#3758): the feed carries
        # deliberate agent announcements only.
        payload = _resolve_audit_payload()
        broadcast_op_id, broadcast_op_class_override = _resolve_op_id_and_class_override(
            method=method,
            path=path,
            payload=payload,
        )
        broadcast_decision: tuple[str, Literal["full", "aggregate"], str] | None = None
        if tenant_id is not None:
            broadcast_decision = await compute_effective_broadcast_detail(
                op_id=broadcast_op_id,
                tenant_id=tenant_id,
                raw_params=payload,
                request_override=read_request_override(),
                op_class_override=broadcast_op_class_override,
            )
            # A route-declared class goes through the same mapping as the
            # other write paths (resolve_op_class).
            # A row with no declared class keeps none: audit query derives
            # it from the op id with the same mapping.
            if "op_class" in payload:
                payload["op_class"] = broadcast_decision[0]
            payload["broadcast_detail_origin"] = broadcast_decision[2]
            # G6.3-T3 (#380): record the resolver's effective detail
            # alongside the origin so ``meho audit query`` can answer
            # both "who/what decided" and "what detail did they get".
            payload["broadcast_detail_effective"] = broadcast_decision[1]

        # ``work_ref_var`` is bound for the whole request by
        # ``_handle_http`` (#1657 / #3804); ``_write_audit_row`` reads it.
        try:
            await _write_audit_row(
                audit_id=audit_id,
                event_class=event_class,
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
        _add_audit_id_header(buffered, classified_record_id(operator_sub) or audit_id)
        for message in buffered:
            await send(message)
