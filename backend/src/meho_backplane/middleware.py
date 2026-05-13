# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Request-context middleware (pure ASGI) and operator-identity binding.

Two concerns share this module: the request-scoped contextvar lifecycle
that every authenticated route inherits, and the small dependency
wrapper that binds operator identity into that same context after JWT
validation succeeds. Co-locating them keeps the contextvar contract
auditable in one file — the middleware sets up and tears down the
``request_id`` / ``operator_sub`` / ``tenant_id`` slots; the wrapper
populates the ``operator_sub`` and ``tenant_id`` slots at the
integration boundary where verify_jwt returns a trusted :class:`Operator`.

Request-context middleware (:class:`RequestContextMiddleware`)
==============================================================

For every HTTP request the middleware:

1. Extracts an incoming ``X-Request-Id`` header value, or generates a
   fresh UUID4 when absent. The value becomes the request's stable
   correlation id, surfaced in:

   - structlog's contextvars (every log emitted by the handler /
     downstream code automatically carries ``request_id``),
   - the ``X-Request-Id`` response header (so the CLI / smoke tests can
     correlate a 5xx back to the exact log line),
   - the structured ``request_completed`` log line emitted by the
     middleware after the handler returns.

2. Times the request via :func:`time.monotonic` and exposes the
   duration in milliseconds on the ``request_completed`` log.

3. Increments the :data:`HTTP_REQUESTS_TOTAL` counter labelled by
   ``method``, ``path``, ``status``. ``path`` is the matched FastAPI
   route template when available (e.g. ``/items/{item_id}``), bounding
   label cardinality; the literal request path is used as a fall-back
   for unmatched routes (404s).

4. Never logs the values of sensitive request headers
   (``Authorization``, ``Cookie``, ``X-API-Key``). The middleware does
   not touch the headers individually for logging; this is enforced by
   the simple invariant that the only request fields it does log are
   ``method``, ``path``, ``status``, and ``duration_ms``. The redaction
   contract is asserted in :mod:`tests.test_observability` by sending
   real header values and grep-ing the captured log.

Pure-ASGI rather than ``BaseHTTPMiddleware`` is the deliberate choice
per the Starlette 1.0+ docs (BaseHTTPMiddleware spawns an anyio task
wrapper that interferes with streaming responses and complicates
contextvar lifetimes). The pattern below is the canonical "wrap
``send`` to mutate the response start" recipe.

Operator-identity binding (:func:`verify_jwt_and_bind`)
=======================================================

Authenticated routes declare ``Depends(verify_jwt_and_bind)`` instead
of ``Depends(verify_jwt)`` directly. The wrapper delegates to
:func:`~meho_backplane.auth.jwt.verify_jwt` for the actual security
work (signature, claims, JWKS rotation) and — only on success — binds
``operator_sub`` and ``tenant_id`` into structlog's contextvars. Every
log line emitted *after* this point under the same request context
(handler logs, the middleware's own ``request_completed`` line, the
audit middleware's ``audit_write_failed`` line) automatically carries
the operator's stable subject id and the tenant the request is scoped
to. ``tenant_id`` is bound as ``str(operator.tenant_id)`` rather than
the raw :class:`uuid.UUID`: structlog's :class:`JSONRenderer` cannot
serialise ``UUID`` natively (it raises ``TypeError`` at the bottom of
the JSON dump), and the string canonical form is what every downstream
log consumer (Loki query, jq filter, audit-row backfill) expects.
:class:`~meho_backplane.audit.AuditMiddleware` re-parses the string
back into a UUID before writing the audit row.

The binding lives in a dependency wrapper rather than the middleware
itself because the middleware is pure ASGI and runs *before* FastAPI
has resolved the request to a route — so it does not yet know whether
the route requires auth or which dependency would extract the
``Operator``. ``verify_jwt`` stays free of side effects so its unit
tests do not need to mock contextvars; the binding side effect is
introduced at the integration boundary, where it belongs.

The middleware's :func:`structlog.contextvars.clear_contextvars` call
at request entry is what guarantees ``operator_sub`` and ``tenant_id``
do not leak across requests served on the same asyncio task.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import uuid4

import structlog
from fastapi import Depends
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from meho_backplane.auth.jwt import verify_jwt
from meho_backplane.auth.operator import Operator
from meho_backplane.metrics import HTTP_REQUESTS_TOTAL

__all__ = [
    "SENSITIVE_HEADERS",
    "RequestContextMiddleware",
    "verify_jwt_and_bind",
]

#: Lower-cased header names whose values must never appear in logs or
#: metrics. The set is intentionally tiny — every entry is paid for in
#: review attention. Add new entries only when an explicit secret-bearing
#: header is introduced (e.g. ``X-Vault-Token`` in G2.2).
SENSITIVE_HEADERS: Final[frozenset[bytes]] = frozenset(
    {b"authorization", b"cookie", b"x-api-key"},
)

_REQUEST_ID_HEADER: Final[bytes] = b"x-request-id"


def _extract_request_id(scope: Scope) -> str:
    """Return the incoming ``X-Request-Id`` value or a fresh UUID4 hex."""
    for name, value in scope.get("headers", ()):
        if name.lower() == _REQUEST_ID_HEADER:
            decoded: str = value.decode("latin-1").strip()
            if decoded:
                return decoded
    return uuid4().hex


def _matched_route_path(scope: Scope) -> str:
    """Return the matched route template, falling back to the literal path.

    FastAPI populates ``scope["route"]`` once the router has resolved
    the request to an ``APIRoute``. Bounding the metrics ``path`` label
    by the route template (``/items/{id}``) instead of the literal URL
    (``/items/42``, ``/items/43``, …) prevents unbounded label
    cardinality — a Prometheus anti-pattern that has caused real
    outages in production deployments.
    """
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    fallback = scope.get("path", "")
    return fallback if isinstance(fallback, str) else ""


class RequestContextMiddleware:
    """Pure-ASGI request-context middleware.

    Stateless beyond ``self.app`` — every request lives in its own
    closure scope. Required by the ASGI contract: a single middleware
    instance handles concurrent requests.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _extract_request_id(scope)

        # Bind into structlog's contextvars so handler-side log calls
        # automatically include ``request_id``. ``clear_contextvars``
        # is critical: without it, contextvars from a previous request
        # served on the same asyncio task could leak into this one.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.monotonic()
        status_code: int = 0
        request_id_bytes = request_id.encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 0))
                # Append (don't replace) — preserve any X-Request-Id
                # the handler explicitly set. In practice handlers
                # don't set this header; the assignment is defensive.
                headers = list(message.get("headers", ()))
                headers.append((_REQUEST_ID_HEADER, request_id_bytes))
                message["headers"] = headers
            await send(message)

        log = structlog.get_logger()
        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            duration_ms = round((time.monotonic() - start) * 1000, 2)
            log.exception(
                "request_failed",
                method=scope.get("method", ""),
                path=_matched_route_path(scope),
                duration_ms=duration_ms,
            )
            raise

        duration_ms = round((time.monotonic() - start) * 1000, 2)
        method = scope.get("method", "")
        path = _matched_route_path(scope)

        HTTP_REQUESTS_TOTAL.labels(
            method=method,
            path=path,
            status=str(status_code),
        ).inc()

        log.info(
            "request_completed",
            method=method,
            path=path,
            status=status_code,
            duration_ms=duration_ms,
        )


async def verify_jwt_and_bind(
    operator: Operator = Depends(verify_jwt),
) -> Operator:
    """Validate the Bearer token and bind ``operator_sub`` + ``tenant_id``.

    Authenticated routes use this wrapper as their security dependency
    in place of :func:`~meho_backplane.auth.jwt.verify_jwt`::

        @router.get("/protected")
        async def protected(
            operator: Operator = Depends(verify_jwt_and_bind),
        ) -> ...:
            ...

    The wrapper relies on FastAPI's dependency-graph contract: ``verify_jwt``
    runs first; only if it returns successfully does this function execute,
    so a failed JWT validation never bleeds operator identity into a later
    request. The bound values are the OIDC ``sub`` claim — the stable
    operator identifier — and the ``tenant_id`` claim that scopes every
    per-tenant feature downstream of this point. ``name`` and ``email``
    are deliberately *not* bound, because they are soft identity claims
    that can vary across token refreshes and would noise the logs without
    adding traceability beyond what ``sub`` already provides.
    ``tenant_role`` is also intentionally absent: role authorisation is
    enforced at the dependency layer (G0.1-T4 ``require_role``), and
    binding it into log context would invite handlers to read it from
    contextvars instead of via the typed :class:`Operator` they receive
    by injection — degrading the typed-injection contract for no log
    value the ``operator_sub`` already lacks.

    ``tenant_id`` is bound as ``str(operator.tenant_id)`` because
    structlog's :class:`structlog.processors.JSONRenderer` ultimately
    delegates to :func:`json.dumps`, which cannot serialise
    :class:`uuid.UUID` natively (it raises ``TypeError``). The string
    canonical form is what every downstream consumer expects (Loki
    queries, jq filters, audit-row backfill scripts);
    :class:`~meho_backplane.audit.AuditMiddleware` parses the string
    back into a :class:`uuid.UUID` before writing the audit row.

    The middleware's request-entry :func:`structlog.contextvars.clear_contextvars`
    call is what guarantees the bound keys do not leak into the next
    request reusing the same asyncio task. We deliberately do **not**
    unbind here — the contextvars live only as long as the current
    request's ASGI scope, and clearing once per request entry is both
    cheaper and more robust than asymmetric unbind logic that would have
    to run in a finally clause inside the wrapper.

    Returns:
        The same :class:`Operator` that ``verify_jwt`` produced — the
        wrapper is transparent to the route handler.
    """
    structlog.contextvars.bind_contextvars(
        operator_sub=operator.sub,
        tenant_id=str(operator.tenant_id),
        target_id=None,  # slot; resolve_target mutates on success (G0.3-T4)
    )
    return operator
