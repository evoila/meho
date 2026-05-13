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
responses are kilobytes); v0.1 does not need streaming-response
support on audited routes and the audit middleware would degrade
streaming semantics regardless. When v0.2 adds streaming routes, the
audit row must be written *after* the response body has streamed —
a change tracked as a follow-up rather than landing here.

References
----------
* https://www.starlette.io/middleware/ — pure-ASGI vs BaseHTTPMiddleware
* https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html — async session lifecycle
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meho_backplane.broadcast import (
    BroadcastEvent,
    classify_op,
    publish_event,
    redact_payload,
)
from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import AuditLog

__all__ = ["AuditMiddleware"]

#: Body returned when the audit insert fails (fail-closed 500). Pinned
#: as a module constant so the contract is greppable from tests.
_AUDIT_FAILURE_BODY: Final[bytes] = json.dumps({"detail": "audit_write_failed"}).encode("utf-8")

_RESPONSE_START: Final[str] = "http.response.start"
_RESPONSE_BODY: Final[str] = "http.response.body"


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
    if not isinstance(raw, str):
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, AttributeError):
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
) -> dict[str, Any]:
    """Open a session and commit one ``AuditLog`` row.

    Lifted out of :meth:`AuditMiddleware.__call__` so the call
    site stays focused on response-buffering control flow. Exceptions
    propagate to the caller, which converts them into the fail-closed
    500 path.

    Reads the payload via :func:`_resolve_audit_payload` so routes
    that bound ``audit_*`` contextvars (G0.4-T5 #262 binds
    ``audit_query_hash`` / ``audit_source`` / ``audit_kind`` /
    ``audit_hit_count`` on ``POST /api/v1/retrieve``) get their
    enrichment without per-route audit code. Routes that bind nothing
    fall back to the empty-dict behaviour every chassis-era surface
    relies on.

    ``audit_id`` is pre-generated by the caller so that the G6.1-T3
    publish-on-write hook can reference the same id on the
    :class:`~meho_backplane.broadcast.events.BroadcastEvent` it emits
    after the audit commit succeeds. Returns the resolved payload so
    the caller can reuse it on the broadcast side without re-walking
    the contextvars (a second walk would race with any unbind that
    happens between the audit write and the publish).
    """
    payload = _resolve_audit_payload()
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=audit_id,
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            target_id=target_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            duration_ms=Decimal(str(duration_ms)),
            payload=payload,
        )
        session.add(row)
        await session.commit()
    return payload


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


async def _publish_broadcast_event(
    *,
    audit_id: uuid.UUID,
    tenant_id: uuid.UUID,
    operator_sub: str,
    method: str,
    path: str,
    status_code: int,
    payload: dict[str, Any],
    handler_exc: BaseException | None,
) -> None:
    """Build the per-request :class:`BroadcastEvent` and publish it.

    The op_id heuristic for chassis HTTP routes is
    ``f"http.{method.lower()}:{path}"`` — out of scope per the task
    body for the connector-shaped per-op-id (T3 acceptance defers
    that to v0.2.next). The ``:`` separator avoids accidental matches
    against
    :data:`~meho_backplane.broadcast.events._READ_SUFFIXES` /
    :data:`~meho_backplane.broadcast.events._WRITE_SUFFIXES` (so a
    route ending in ``.list`` would otherwise classify as ``read``);
    today's chassis has no such routes, but the separator keeps the
    invariant honest as new routes land.

    Route-level overrides
    ---------------------

    A route may publish under a connector-style ``op_id`` (e.g. the
    G4.3 retrieval-usage route at ``GET /api/v1/retrieve/usage`` binds
    ``audit_op_id="meho.retrieval.usage"`` +
    ``audit_op_class="audit_query"`` so the broadcast classifier sees
    the canonical name instead of the HTTP-shape default). The
    override flows through the existing ``audit_*`` contextvar payload
    mechanism: :func:`_resolve_audit_payload` strips the ``audit_``
    prefix, so ``audit_op_id`` lands in ``payload`` as ``op_id``. This
    helper prefers the override when present and falls back to the
    HTTP heuristic otherwise. Same shape for ``op_class`` — required
    because :func:`classify_op` would otherwise classify
    ``meho.retrieval.usage`` as ``other`` (no ``audit.`` prefix, no
    read/write verb suffix) and broadcast the full request payload,
    defeating the aggregate-only-for-audit-query discipline in
    decision #3 of ``docs/planning/v0.2-decisions.md``.

    The override is per-route opt-in; routes that don't bind these
    contextvars (every chassis-era surface) get the unchanged
    HTTP-shape + :func:`classify_op` behaviour.

    ``principal_name`` / ``target_name`` are ``None`` for HTTP-path
    events — the JWT name claim isn't bound to contextvars by
    :func:`~meho_backplane.middleware.verify_jwt_and_bind` (only
    ``operator_sub`` and ``tenant_id`` are), and the connector
    ``target_name`` doesn't apply to chassis routes. T5 (CLI watch)
    + T6 (MCP resource) will read events back from the stream and
    JOIN against ``audit_log`` by ``audit_id`` for any enrichment
    a downstream consumer needs.
    """
    op_id_override = payload.get("op_id")
    op_id = (
        op_id_override
        if isinstance(op_id_override, str) and op_id_override
        else f"http.{method.lower()}:{path}"
    )
    op_class_override = payload.get("op_class")
    op_class = (
        op_class_override
        if isinstance(op_class_override, str) and op_class_override
        else classify_op(op_id)
    )
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
        payload=redact_payload(op_class, payload, result_status),
    )
    await publish_event(event)


async def _run_inner_app_buffered(
    app: ASGIApp,
    scope: Scope,
    receive: Receive,
    buffered: list[Message],
) -> tuple[int, BaseException | None]:
    """Invoke the inner app capturing its send messages.

    Returns ``(status_code, handler_exc)``. When the inner app raises a
    non-cancellation :class:`Exception`, the buffered messages are
    cleared (a partially-emitted response cannot be safely forwarded)
    and ``status_code`` is synthesised to 500 so the audit row reflects
    Starlette's :class:`~starlette.middleware.errors.ServerErrorMiddleware`
    eventual verdict. ``CancelledError`` is intentionally *not* caught
    so client disconnects still cancel the task tree cleanly.
    """
    status_code: int = 0

    async def buffered_send(message: Message) -> None:
        nonlocal status_code
        if message["type"] == _RESPONSE_START:
            status_code = int(message.get("status", 0))
        buffered.append(message)

    try:
        await app(scope, receive, buffered_send)
    except Exception as exc:
        buffered.clear()
        return 500, exc
    return status_code, None


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
        # fresh 500. v0.1 routes return single-shot JSON responses; the
        # buffer is small. The inner-app invocation is wrapped so a
        # handler exception still produces an audit row for the failed
        # action (the audit row is the operator-facing trace of "this
        # action was attempted"); see :func:`_run_inner_app_buffered`.
        buffered: list[Message] = []
        status_code, handler_exc = await _run_inner_app_buffered(
            self.app,
            scope,
            receive,
            buffered,
        )

        duration_ms = round((time.monotonic() - start) * 1000, 2)
        operator_sub = structlog.contextvars.get_contextvars().get("operator_sub")

        if not isinstance(operator_sub, str) or not operator_sub:
            # No operator to attribute. Skip the audit write entirely
            # (public surfaces, 401s, and any other unauthenticated
            # path land here); forward the buffered response unchanged
            # — or, if the handler raised, re-raise so the outer
            # ServerErrorMiddleware builds the 500.
            if handler_exc is not None:
                raise handler_exc
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
        audit_id = uuid.uuid4()
        target_id = _resolve_target_id()
        try:
            payload = await _write_audit_row(
                audit_id=audit_id,
                operator_sub=operator_sub,
                tenant_id=tenant_id,
                target_id=target_id,
                method=method,
                path=path,
                status_code=status_code,
                request_id=request_id,
                duration_ms=duration_ms,
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
        # tenant_id but the broadcast event requires a UUID).
        if tenant_id is not None:
            await _publish_broadcast_event(
                audit_id=audit_id,
                tenant_id=tenant_id,
                operator_sub=operator_sub,
                method=method,
                path=path,
                status_code=status_code,
                payload=payload,
                handler_exc=handler_exc,
            )

        # If the handler raised, re-raise so the outer
        # ServerErrorMiddleware builds the canonical 500; the audit
        # row attributing the failed action is already persisted,
        # satisfying the "every authenticated action gets exactly one
        # row" contract for the 5xx path.
        if handler_exc is not None:
            raise handler_exc

        # Audit committed and handler succeeded — forward the buffered
        # response verbatim.
        for message in buffered:
            await send(message)
