# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Collect one ``request_finished`` event per REST or MCP boundary (#3734).

How it works
------------

:class:`~meho_backplane.audit.AuditMiddleware` is the outermost place
that sees every HTTP request, including ``POST /mcp``. It calls
:func:`open_request_usage` when a request starts and
:meth:`RequestUsageBoundary.close` when it ends. ``close`` writes one
``request_finished`` row, plus one ``context_selected`` row for each
selection a handler noted.

One event per boundary
----------------------

The open boundary lives in a :class:`~contextvars.ContextVar`. When a
boundary is already open, :func:`open_request_usage` returns ``None``, so
a nested handler (or a second middleware layer) cannot open a second
boundary and cannot write a second ``request_finished`` event. Code inside
the request only *annotates* the open boundary:

* :func:`note_request_route` -- the MCP layer names the JSON-RPC method,
  the tool, or the resource template the request used.
* :func:`note_request_status` -- the MCP audit writer records the
  projected status (MCP answers HTTP 200 even when a call fails).
* :func:`note_context_selected` -- optional; records that context was
  picked, on an opaque correlation id.

Never blocks a request
----------------------

* With ``REQUEST_USAGE_RETENTION_DAYS`` unset, :func:`open_request_usage`
  returns ``None`` and nothing else runs.
* A write failure is logged (exception class only) and swallowed. It
  never changes the response, the audit write, or a policy decision.
* Unauthenticated requests write nothing: there is no tenant or
  principal to attribute.

What is stored is fixed by
:class:`~meho_backplane.request_usage.schema.RequestUsageEventData`.
"""

from __future__ import annotations

import time
import uuid
import weakref
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

import structlog
from starlette.types import Message, Scope, Send

from meho_backplane.db.engine import get_sessionmaker
from meho_backplane.db.models import RequestUsageEvent
from meho_backplane.request_usage.schema import (
    CATALOG_CONTEXT_KEY,
    UNMATCHED_ROUTE,
    RequestUsageEventData,
    RouteCatalog,
    Transport,
    outcome_for_status,
)
from meho_backplane.settings import get_settings

__all__ = [
    "MAX_CONTEXT_SELECTIONS",
    "RequestUsageBoundary",
    "build_route_catalog",
    "collection_enabled",
    "note_context_selected",
    "note_request_route",
    "note_request_status",
    "open_request_usage",
]

_log = structlog.get_logger(__name__)

#: Upper bound on ``context_selected`` events per request, so one request
#: cannot write an unbounded number of rows.
MAX_CONTEXT_SELECTIONS: Final[int] = 16

_MCP_PATH_PREFIX: Final[str] = "/mcp"

#: REST route templates per app object. The route table is fixed once the
#: app is built, so it is read once per app. Weak keys: a dropped app (tests
#: build many) takes its entry with it.
_REST_ROUTES_BY_APP: weakref.WeakKeyDictionary[Any, frozenset[str]] = weakref.WeakKeyDictionary()


@dataclass(slots=True)
class _BoundaryState:
    """What the request has told the open boundary so far."""

    started: float
    http_status: int = 0
    route: str | None = None
    projected_status: int | None = None
    context_selections: list[uuid.UUID] = field(default_factory=list)


_current_boundary: ContextVar[_BoundaryState | None] = ContextVar(
    "request_usage_boundary",
    default=None,
)


def collection_enabled() -> bool:
    """Return whether request usage collection is on (retention is set).

    Settings that cannot load count as "off": usage collection must never
    be the reason a request fails (for example a public route served
    before the rest of the configuration is valid).
    """
    try:
        return get_settings().request_usage_retention_days is not None
    except Exception:
        return False


def note_request_route(route: str) -> None:
    """Label the open boundary with *route*; no-op when none is open.

    Callers pass only labels built from registered names (``tool:<name>``
    after the tool lookup succeeded, ``resource:<template>`` after a
    template matched, ``method:<name>`` for a registered method). The
    collector checks the label against the catalog again before it
    stores it. The last call wins, so the outermost handler's label is
    the one kept.
    """
    state = _current_boundary.get()
    if state is not None:
        state.route = route


def note_request_status(status_code: int) -> None:
    """Record the projected status of an MCP call on the open boundary."""
    state = _current_boundary.get()
    if state is not None:
        state.projected_status = status_code


def note_context_selected(correlation_id: uuid.UUID | None = None) -> uuid.UUID | None:
    """Record that context was selected for this request.

    Optional. The event only says context was picked; it is never a
    claim that the agent used or followed it. Returns the correlation
    id (a fresh opaque id when *correlation_id* is ``None``), or
    ``None`` when no boundary is open or the per-request cap is hit.
    """
    state = _current_boundary.get()
    if state is None or len(state.context_selections) >= MAX_CONTEXT_SELECTIONS:
        return None
    selected = correlation_id or uuid.uuid4()
    state.context_selections.append(selected)
    return selected


def _rest_routes_for(app: Any) -> frozenset[str]:
    """Return the route templates of *app* (cached per app object)."""
    if app is None:
        return frozenset()
    cached = _REST_ROUTES_BY_APP.get(app)
    if cached is not None:
        return cached
    from fastapi.routing import iter_route_contexts

    templates = frozenset(
        path
        for context in iter_route_contexts(getattr(app, "routes", []))
        if isinstance(path := getattr(context, "path_format", None), str)
    )
    _REST_ROUTES_BY_APP[app] = templates
    return templates


def _mcp_routes() -> frozenset[str]:
    """Return every registered MCP method, tool and resource label."""
    # Imported here: the MCP modules import this module.
    from meho_backplane.mcp.registry import (
        registered_resource_templates,
        registered_tool_names,
    )
    from meho_backplane.mcp.server import registered_methods

    return frozenset(
        [
            *(f"method:{name}" for name in registered_methods()),
            *(f"tool:{name}" for name in registered_tool_names()),
            *(f"resource:{template}" for template in registered_resource_templates()),
        ],
    )


def build_route_catalog(app: Any) -> RouteCatalog:
    """Build the route catalog for *app* and the live MCP registries."""
    return RouteCatalog(rest=_rest_routes_for(app), mcp=_mcp_routes())


def _uuid_or_none(raw: object) -> uuid.UUID | None:
    """Parse *raw* as a UUID; anything else becomes ``None``."""
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


def _route_label(scope: Scope, state: _BoundaryState, transport: Transport) -> str:
    """Return the route label to store, before the catalog check."""
    if transport == "mcp":
        return state.route or UNMATCHED_ROUTE
    route = scope.get("route")
    template = getattr(route, "path_format", None)
    return template if isinstance(template, str) and template else UNMATCHED_ROUTE


def _lineage_ids(contextvars: dict[str, Any]) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Return ``(session_id, run_id)`` for the finished request."""
    # Imported here to keep this module light for the MCP import chain.
    from meho_backplane.operations._audit import agent_session_id_var, run_id_var

    session_id = _uuid_or_none(contextvars.get("mcp_session_id")) or agent_session_id_var.get()
    return session_id, run_id_var.get()


class RequestUsageBoundary:
    """The open boundary of one request. Created by :func:`open_request_usage`."""

    __slots__ = ("_state", "_token")

    def __init__(self, state: _BoundaryState, token: Token[_BoundaryState | None]) -> None:
        self._state = state
        self._token = token

    def wrap_send(self, send: Send) -> Send:
        """Return a ``send`` that records the status the client really gets."""
        state = self._state

        async def recording_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                state.http_status = int(message.get("status", 0))
            await send(message)

        return recording_send

    async def close(self, scope: Scope) -> None:
        """Close the boundary and write its events. Never raises ``Exception``."""
        try:
            _current_boundary.reset(self._token)
        except ValueError:
            # Closed from another context: still clear this one.
            _current_boundary.set(None)
        try:
            await self._write(scope)
        except Exception as exc:
            # Only the class name: driver errors can echo statement parameters.
            _log.warning("request_usage_write_failed", error_type=type(exc).__name__)

    def _events(self, scope: Scope) -> list[RequestUsageEventData]:
        """Build the validated events for this request (empty when unattributable)."""
        contextvars = structlog.contextvars.get_contextvars()
        principal_sub = contextvars.get("operator_sub")
        tenant_id = _uuid_or_none(contextvars.get("tenant_id"))
        if not isinstance(principal_sub, str) or not principal_sub or tenant_id is None:
            return []

        state = self._state
        path = scope.get("path", "")
        transport: Transport = (
            "mcp" if isinstance(path, str) and path.startswith(_MCP_PATH_PREFIX) else "rest"
        )
        catalog = build_route_catalog(scope.get("app"))
        route = _route_label(scope, state, transport)
        if not catalog.allows(transport, route):
            route = UNMATCHED_ROUTE
        status = state.projected_status if state.projected_status is not None else state.http_status
        session_id, run_id = _lineage_ids(contextvars)
        common: dict[str, Any] = {
            "occurred_at": datetime.now(UTC),
            "tenant_id": tenant_id,
            "principal_sub": principal_sub,
            "session_id": session_id,
            "run_id": run_id,
            "request_id": _uuid_or_none(contextvars.get("request_id")),
            "transport": transport,
            "route": route,
        }
        context = {CATALOG_CONTEXT_KEY: catalog}
        events = [
            RequestUsageEventData.model_validate(
                {
                    **common,
                    "id": uuid.uuid4(),
                    "kind": "request_finished",
                    "outcome": outcome_for_status(status),
                    "duration_ms": round((time.monotonic() - state.started) * 1000, 2),
                },
                context=context,
            ),
        ]
        events.extend(
            RequestUsageEventData.model_validate(
                {
                    **common,
                    "id": uuid.uuid4(),
                    "kind": "context_selected",
                    "correlation_id": correlation_id,
                },
                context=context,
            )
            for correlation_id in state.context_selections
        )
        return events

    async def _write(self, scope: Scope) -> None:
        """Insert this request's events in one short transaction."""
        events = self._events(scope)
        if not events:
            return
        async with get_sessionmaker()() as session:
            for event in events:
                row = event.model_dump()
                if row["duration_ms"] is not None:
                    row["duration_ms"] = Decimal(str(row["duration_ms"]))
                session.add(RequestUsageEvent(**row))
            await session.commit()


def open_request_usage() -> RequestUsageBoundary | None:
    """Open the request boundary, or return ``None``.

    ``None`` when collection is off, or when a boundary is already open in
    this context (a nested handler or middleware layer). Only the code
    that got a boundary may close it, so each request writes at most one
    ``request_finished`` event.
    """
    if not collection_enabled() or _current_boundary.get() is not None:
        return None
    state = _BoundaryState(started=time.monotonic())
    return RequestUsageBoundary(state, _current_boundary.set(state))
