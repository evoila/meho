# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Decide which REST and MCP request rows still go into ``audit_log`` (#3757).

Before this change, every authenticated REST request and every MCP tool
call or resource read wrote one ``legacy_request`` row. Most of those rows
are routine: a list, a search, a page view. They made the audit counters
mean "requests", not "actions and decisions".

With ``AUDIT_WRAPPER_ROWS_RETIRED=true`` the two wrapper writers
(:class:`~meho_backplane.audit.AuditMiddleware` and the MCP handlers that
call :func:`~meho_backplane.mcp.audit.write_mcp_audit_row`) ask
:func:`wrapper_row_class` what to write. The rules, in order:

1. A classified writer already wrote a row for this request in the
   caller's name (the dispatcher, an approval decision, a topology edit,
   ...): write nothing. That row is the record; a wrapper row would count
   it twice. A row under another identity (a system job's name) does not
   count: the wrapper row stays, so the record still says who called.
2. The request was refused (HTTP 401 / 403): write a ``policy_decision``
   row. A refusal is a decision, so it stays on the record.
3. The request is routine (a read, a search, a list, a resource read, a
   preview, a heartbeat) or ran an operation through the dispatcher:
   write nothing. The request usage event (#3734) records it.
4. Any other 4xx (validation failure, not found, conflict): write
   nothing. Nothing changed.
5. Anything else is a MEHO change with no record of its own (create a
   target, enable a connector, ...), or a failed one (5xx): write an
   ``operation_execution`` row.

With the setting off (the default), every wrapper row is still written as
``legacy_request``, exactly as before.

Fail-closed gate
----------------

Routine activity is recorded only as request usage events after the
cutover, and those are written only when ``REQUEST_USAGE_RETENTION_DAYS``
is set. :class:`~meho_backplane.settings.Settings` therefore refuses to
start with the setting on and usage collection off. The code-side gates
(every writer declares its class, readers moved to usage events, routine
route lists match the app) are pinned by ``tests/test_audit_wrapper_cutover.py``.

How rule 1 knows
----------------

The wrapper opens a scope (:func:`open_classified_record_scope`) for one
request or one MCP call. A SQLAlchemy ``init`` listener on
:class:`~meho_backplane.db.models.AuditLog` records, on the scope, the
``operator_sub`` of every row built with a class other than
``legacy_request``. The wrapper then asks whether one of them is the
caller's. The scope object is shared by reference, so code that runs in a
copied context (a thread pool, a task group) still marks the same scope.

What a kept record carries
--------------------------

The wrapper row carried the request's lineage: the agent session, the
delegated actor (RFC 8693 ``act.sub``), the parent record, the request id
and the work reference. Many domain writers (a connector edit, a topology
edit, ...) leave those columns empty. After the cutover their row is the
only one, so the same listener fills each empty lineage column from the
request context. A value the writer set is kept. Outside a scope (a
system job, an async run that finishes after the response) nothing is
filled; with the setting off nothing is filled either.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Final

from sqlalchemy import event

from meho_backplane.auth.delegation import resolve_actor_sub
from meho_backplane.db.models import AuditEventClass, AuditLog
from meho_backplane.operations._audit import (
    parent_audit_id_var,
    resolve_agent_session_id,
    work_ref_var,
)
from meho_backplane.request_usage.collector import current_request_id
from meho_backplane.settings import get_settings

__all__ = [
    "DISPATCH_ROUTES",
    "MCP_ROUTINE_OP_CLASSES",
    "ROUTINE_POST_ROUTES",
    "classified_record_seen",
    "close_classified_record_scope",
    "mcp_tool_is_routine",
    "open_classified_record_scope",
    "rest_request_is_routine",
    "wrapper_row_class",
    "wrapper_rows_retired",
]

#: HTTP methods that never change state.
_SAFE_METHODS: Final[frozenset[str]] = frozenset({"GET", "HEAD", "OPTIONS"})

#: POST route templates that only read (searches, queries, previews) or are
#: machine reports (a liveness heartbeat, a runner's check results).
#: ``tests/test_audit_wrapper_cutover.py`` checks that each one exists.
ROUTINE_POST_ROUTES: Final[frozenset[str]] = frozenset(
    {
        "/api/v1/addons/pairings/{name}/heartbeat",
        "/api/v1/ask_docs",
        "/api/v1/audit/class-counts",
        "/api/v1/audit/query",
        "/api/v1/checks/results",
        "/api/v1/memory/search",
        "/api/v1/operations/preview",
        "/api/v1/operations/result-query",
        "/api/v1/read_docs",
        "/api/v1/retrieve",
        "/api/v1/retrieve/eval",
        "/api/v1/retrieve/retire-checklist",
        "/api/v1/search_docs",
        "/ui/connectors/import",
        "/ui/conventions/preview",
        "/ui/corpus/chunks/{collection_key}/{chunk_id}",
        "/ui/corpus/search",
        "/ui/kb/editor-preview",
        "/ui/kb/search",
        "/ui/memory/preview",
        "/ui/operations/preview",
        "/ui/retrieval/diagnostics",
        "/ui/retrieval/eval",
        "/ui/retrieval/retire-checklist",
        "/ui/retrieval/usage",
        "/ui/runbooks/preview",
        "/ui/scheduler/validate-cron",
    },
)

#: Routes that run an operation through the dispatcher. The dispatcher
#: writes the execution or policy row; an async run writes it later, after
#: the response, so rule 1 cannot see it yet.
DISPATCH_ROUTES: Final[frozenset[str]] = frozenset(
    {"/api/v1/operations/call", "/ui/operations/call"},
)

#: MCP tool ``op_class`` values that never need a wrapper row: reads, audit
#: queries, and ``call_operation`` (``tool_call``), whose record is the
#: dispatcher's.
MCP_ROUTINE_OP_CLASSES: Final[frozenset[str]] = frozenset({"read", "audit_query", "tool_call"})

#: HTTP statuses that mean "refused".
_DENIED_STATUSES: Final[frozenset[int]] = frozenset({401, 403})


class _ClassifiedRecords:
    """Mutable marker for one request: whose classified rows were built?"""

    __slots__ = ("operator_subs",)

    def __init__(self) -> None:
        self.operator_subs: set[str] = set()


_current_scope: ContextVar[_ClassifiedRecords | None] = ContextVar(
    "audit_classified_record_scope",
    default=None,
)


def wrapper_rows_retired() -> bool:
    """Return whether ``AUDIT_WRAPPER_ROWS_RETIRED`` is on.

    Settings that cannot load count as "off", so the fallback is the old
    behaviour (write every wrapper row), never a silent gap.
    """
    try:
        return get_settings().audit_wrapper_rows_retired
    except Exception:
        return False


def open_classified_record_scope() -> Token[_ClassifiedRecords | None]:
    """Start watching for classified rows in this request or MCP call."""
    return _current_scope.set(_ClassifiedRecords())


def close_classified_record_scope(token: Token[_ClassifiedRecords | None]) -> None:
    """Stop watching; restore the outer scope."""
    try:
        _current_scope.reset(token)
    except ValueError:
        # Reset from another context: still clear this one.
        _current_scope.set(None)


def classified_record_seen(operator_sub: str) -> bool:
    """Return whether code in the open scope built a classified row for this caller."""
    scope = _current_scope.get()
    return scope is not None and operator_sub in scope.operator_subs


def _request_lineage() -> dict[str, Any]:
    """Return the lineage columns the wrapper row would carry for this request."""
    return {
        "agent_session_id": resolve_agent_session_id(),
        "actor_sub": resolve_actor_sub(),
        "parent_audit_id": parent_audit_id_var.get(),
        "request_id": current_request_id(),
        "work_ref": work_ref_var.get(),
    }


def _fill_request_lineage(kwargs: dict[str, Any]) -> None:
    """Fill each lineage column the writer left empty from the request context."""
    for column, value in _request_lineage().items():
        if value is not None and kwargs.get(column) is None:
            kwargs[column] = value


@event.listens_for(AuditLog, "init")
def _note_classified_record(_target: AuditLog, _args: Any, kwargs: dict[str, Any]) -> None:
    """Record whose row it is when a row with a non-legacy class is built.

    After the cutover the row also inherits the request's lineage (see the
    module docstring). SQLAlchemy passes ``kwargs`` so that the listener
    may change it in place before ``__init__`` runs.
    """
    scope = _current_scope.get()
    if scope is None:
        return
    event_class = kwargs.get("event_class")
    operator_sub = kwargs.get("operator_sub")
    if (
        event_class is None
        or event_class == AuditEventClass.LEGACY_REQUEST
        or not isinstance(operator_sub, str)
    ):
        return
    scope.operator_subs.add(operator_sub)
    if wrapper_rows_retired():
        _fill_request_lineage(kwargs)


def rest_request_is_routine(method: str, route_template: str | None) -> bool:
    """Return whether a REST request needs no wrapper row when it is not refused."""
    if method.upper() in _SAFE_METHODS:
        return True
    return route_template in ROUTINE_POST_ROUTES or route_template in DISPATCH_ROUTES


def mcp_tool_is_routine(op_class: str) -> bool:
    """Return whether an MCP tool call needs no wrapper row when it is not refused."""
    return op_class in MCP_ROUTINE_OP_CLASSES


def wrapper_row_class(
    *,
    status_code: int,
    routine: bool,
    operator_sub: str,
) -> AuditEventClass | None:
    """Return the class of the wrapper row to write, or ``None`` for no row.

    See the module docstring for the rules. ``operator_sub`` is the caller
    the wrapper row would name; only a classified row in that name counts
    as the record. With the cutover off this is always ``legacy_request``.
    """
    if not wrapper_rows_retired():
        return AuditEventClass.LEGACY_REQUEST
    if classified_record_seen(operator_sub):
        return None
    if status_code in _DENIED_STATUSES:
        return AuditEventClass.POLICY_DECISION
    if routine or 400 <= status_code < 500:
        return None
    return AuditEventClass.OPERATION_EXECUTION
