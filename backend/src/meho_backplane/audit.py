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


async def _write_audit_row(
    *,
    operator_sub: str,
    tenant_id: uuid.UUID | None,
    method: str,
    path: str,
    status_code: int,
    request_id: uuid.UUID | None,
    duration_ms: float,
) -> None:
    """Open a session and commit one ``AuditLog`` row.

    Lifted out of :meth:`AuditMiddleware.__call__` so the call
    site stays focused on response-buffering control flow. Exceptions
    propagate to the caller, which converts them into the fail-closed
    500 path.
    """
    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        row = AuditLog(
            id=uuid.uuid4(),
            occurred_at=datetime.now(UTC),
            operator_sub=operator_sub,
            tenant_id=tenant_id,
            method=method,
            path=path,
            status_code=status_code,
            request_id=request_id,
            duration_ms=Decimal(str(duration_ms)),
            payload={},
        )
        session.add(row)
        await session.commit()


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
        try:
            await _write_audit_row(
                operator_sub=operator_sub,
                tenant_id=tenant_id,
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

        # Audit row was committed. If the handler raised, re-raise so
        # the outer ServerErrorMiddleware builds the canonical 500;
        # the audit row attributing the failed action is already
        # persisted, satisfying the "every authenticated action gets
        # exactly one row" contract for the 5xx path.
        if handler_exc is not None:
            raise handler_exc

        # Audit committed and handler succeeded — forward the buffered
        # response verbatim.
        for message in buffered:
            await send(message)
