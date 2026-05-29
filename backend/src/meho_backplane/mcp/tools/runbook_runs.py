# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
# code-quality-allow: load-bearing tool descriptions per issue #1313; the
# runbook_next description teaches the opacity contract + verify shapes +
# no-skip + single-assignee + no-third-response-shape (~110 lines on its
# own) and is regression-tested verbatim. Splitting would obscure the
# contract. Same shape as sibling mcp/tools/runbooks.py (template-side,
# 686 lines) which made the identical structural choice.

"""``runbook_start`` / ``runbook_next`` / ``runbook_abort`` / ``runbook_reassign`` /
``runbook_list_runs`` MCP tools -- run lifecycle (G12.3-T6).

Five tool registrations that surface
:class:`~meho_backplane.runbooks.run_service.RunbookRunService` on the
MCP transport. The matching REST routes are owned by the sibling Task
#1311; both transports converge on the same service, so the
single-assignee enforcement, the verify-at-advance gating, the
audit-row plumbing, and the response-shape opacity all live in one
place (T3, #1308).

The :data:`_NEXT_DESCRIPTION` text is the longest in the codebase by
design -- it teaches the opacity contract, the verify shapes (``confirm``
and ``operation_call``), the no-skip discipline, the single-assignee
enforcement, and the no-third-response-shape contract. Same call as
:data:`~meho_backplane.mcp.tools.runbooks._EDIT_DESCRIPTION` from #1298:
agent UX lives in the description; if a refactor strips it, the surface
regresses silently. The accompanying regression test in
``test_mcp_tools_runbook_runs.py`` checks the verbatim load-bearing
strings (``OPACITY CONTRACT``, ``WHEN A STEP FAILS``,
``SINGLE-ASSIGNEE``, ``no skip, no force_advance``).

RBAC
====

Four tools are ``OPERATOR``-readable/writable (start / next / abort /
list_runs). :func:`runbook_reassign` is ``TENANT_ADMIN``-only.
Service-level enforces single-assignee inside
:meth:`~meho_backplane.runbooks.run_service.RunbookRunService.next_step`
and :meth:`~meho_backplane.runbooks.run_service.RunbookRunService.abort_run`;
the role gate handles tenant-wide visibility discipline on
:func:`runbook_list_runs` (operator sees only own; admin sees all).

Audit + broadcast
=================

Same dispatcher mechanism as the kb + template tools -- one
``audit_log`` row + one broadcast event per ``tools/call``.
:func:`runbook_next` on an ``operation_call`` verify produces TWO audit
rows: one outer envelope (dispatcher writes it with
``op_class=write``) and one inner verify dispatch (audit op-id is the
verify's ``op_id``, e.g. ``vmware.host.is_powered_on``, with
``run_id`` + ``step_id`` populated via the contextvars the service
binds around :func:`~meho_backplane.operations.meta_tools.call_operation`
per G12.1-T2 / #1294).

Error mapping
=============

All operator-actionable typed exceptions from
:class:`~meho_backplane.runbooks.run_service.RunbookRunService` --
:class:`RunNotFoundError`, :class:`NotRunAssigneeError`,
:class:`RunAlreadyTerminalError`, :class:`PreviousStepFailedError`,
:class:`PreviousStepNotVerifiedError`, :class:`MissingParamsError`,
:class:`VerifyResponseRequiredError`, :class:`VerifyResponseMismatchError`,
:class:`DeprecatedTemplateError`, :class:`TemplateNotFoundError` --
map to :class:`McpInvalidParamsError` (``-32602``). Anything else
bubbles to the dispatcher's generic ``-32603``.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.runbooks.engine import (
    PreviousStepNotVerifiedError,
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
)
from meho_backplane.runbooks.run_service import (
    DeprecatedTemplateError,
    MissingParamsError,
    NotRunAssigneeError,
    PreviousStepFailedError,
    RunAlreadyTerminalError,
    RunbookRunService,
    RunNotFoundError,
)
from meho_backplane.runbooks.runs_schemas import (
    AbortRunRequest,
    ListRunsFilter,
    NextStepRequest,
    ReassignRunRequest,
    StartRunRequest,
)
from meho_backplane.runbooks.service import TemplateNotFoundError

__all__: list[str] = []


#: Op-class strings keep parity with the
#: :mod:`meho_backplane.broadcast.classify` taxonomy. The lifecycle
#: surface is split: ``list_runs`` is a passive read, the other four are
#: writes (``start`` inserts the run row, ``next`` advances state +
#: dispatches verify, ``abort`` flips state + writes audit, ``reassign``
#: mutates ``assigned_to``).
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"

#: The caller-actionable service + engine exceptions that map to
#: JSON-RPC ``-32602``. Same posture as
#: :mod:`meho_backplane.mcp.tools.runbooks` -- typed exceptions become a
#: ``-32602`` (``INVALID_PARAMS``) with a ``"<tool>: <detail>"`` message
#: so the agent gets a self-correcting signal rather than a generic
#: ``-32603`` (``INTERNAL_ERROR``).
#:
#: Left unannotated so mypy infers the precise concrete-member tuple
#: (rather than ``tuple[type[Exception], ...]``) -- the variadic form is
#: rejected as an ``except`` argument.
_INVALID_PARAMS_ERRORS = (
    DeprecatedTemplateError,
    MissingParamsError,
    NotRunAssigneeError,
    PreviousStepFailedError,
    PreviousStepNotVerifiedError,
    RunAlreadyTerminalError,
    RunNotFoundError,
    TemplateNotFoundError,
    VerifyResponseMismatchError,
    VerifyResponseRequiredError,
)

#: Default + maximum row cap for ``runbook_list_runs``. Matches the
#: service's :meth:`~meho_backplane.runbooks.run_service.RunbookRunService.list_runs`
#: default (100).
_DEFAULT_LIST_LIMIT: Final[int] = 100
_MAX_LIST_LIMIT: Final[int] = 500


def _to_invalid_params(tool: str, exc: Exception) -> McpInvalidParamsError:
    """Wrap a caller-actionable service exception as :class:`McpInvalidParamsError`.

    Centralised so every handler maps the typed-exception set in
    :data:`_INVALID_PARAMS_ERRORS` to the spec-correct ``-32602`` with a
    consistent ``"<tool>: <detail>"`` message shape.
    """
    return McpInvalidParamsError(f"{tool}: {exc}")


def _parse_run_id(tool: str, raw: Any) -> uuid.UUID:
    """Parse the ``run_id`` argument as a :class:`uuid.UUID`.

    The wire shape carries ``run_id`` as a string (the inputSchema sets
    ``format: "uuid"`` but the Anthropic Messages API only validates the
    string type, not the format); a malformed value surfaces as
    ``-32602`` rather than a generic ``-32603`` so the agent can
    self-correct.
    """
    try:
        return uuid.UUID(str(raw))
    except (TypeError, ValueError) as exc:
        raise _to_invalid_params(tool, ValueError(f"invalid run_id: {raw!r}")) from exc


# ---------------------------------------------------------------------------
# Tool descriptions (the load-bearing UX)
# ---------------------------------------------------------------------------


_START_DESCRIPTION: Final[str] = (
    "Start a new runbook run.\n\n"
    "Auto-assigns you (the caller) as the assignee. Returns the run_id and "
    "the body of step 1 with ${run.target} and ${run.params.X} "
    "substituted.\n\n"
    'Use when: the human operator says "execute runbook X" or "start '
    'runbook X". You target the latest non-deprecated published version '
    "of the template. If only deprecated versions exist, this fails -- "
    "ask the senior to publish a fresh version.\n\n"
    "Requires OPERATOR role. The run is single-assignee -- only you can "
    "advance it (via runbook_next) until you abort or reassign.\n\n"
    "Errors:\n"
    "- -32602 if the template slug is missing, only-deprecated, or "
    "missing required ${run.params.X} keys."
)

_NEXT_DESCRIPTION: Final[str] = (
    "Advance one step in an in-progress runbook run.\n\n"
    "THE OPACITY CONTRACT: this tool returns the body of ONLY ONE step -- "
    "the one you're currently on. You will NEVER see step 5 while you're "
    "on step 3. There is no overload, no parameter, no flag that returns "
    "more. The substrate is the authority on which step you can see; "
    "what you call `last_verified` is informational only.\n\n"
    "VERIFY SHAPES: every step's `verify` is one of two shapes.\n\n"
    '1. verify.type=\'confirm\': pass verify_response={"type": "confirm", '
    '"answer": "yes" | "no" | "escalate"}.\n'
    '   - "yes" advances. "no" or "escalate" transitions the step '
    "to failed.\n"
    "   - Only the human operator can answer -- do not auto-confirm on "
    "the human's behalf.\n"
    "     If unsure, escalate.\n\n"
    "2. verify.type='operation_call': the substrate dispatches the verify "
    "call itself\n"
    "   and compares the result against `expect` (structural equality + "
    "presence match --\n"
    "   no JSONPath, no operators, no boolean composition; the substrate "
    "is dumb on\n"
    "   purpose, see #1177).\n"
    "   - You pass verify_response=None on `next` calls for operation_call "
    "verifies --\n"
    "     the substrate does the dispatch + match for you.\n"
    "   - On match: step transitions to verified, advances. On mismatch: "
    "step transitions\n"
    "     to failed.\n\n"
    "WHEN A STEP FAILS:\n"
    "- A 'failed' step does NOT auto-advance, does NOT auto-retry. There "
    "is no skip, no force_advance, no override. These DO NOT EXIST.\n"
    "- The only forward path from a 'failed' state is runbook_abort + "
    "(optionally)\n"
    "  runbook_start over with the issue resolved.\n"
    "- This is by design -- runbooks are governance-graded; the substrate "
    "refuses\n"
    "  to advance over an unverified step.\n\n"
    "SINGLE-ASSIGNEE: you can only advance a run that is assigned_to you "
    "(per the\n"
    "run's assigned_to field). Even TENANT_ADMINs cannot bypass this -- "
    "they go through\n"
    "runbook_reassign first to take ownership, then advance.\n\n"
    "WHEN TO USE: every time the human operator confirms the current step "
    "is done\n"
    "(operation_call verify resolves automatically) and the run should "
    "progress.\n\n"
    'WHEN NOT TO USE: never to "skip" a step you don\'t understand. If '
    "the current\n"
    "step is broken or unsafe, runbook_abort with a reason and ask the "
    "senior to\n"
    "fix the template (which will fork-on-edit per the "
    "docs/runbooks/authoring.md\n"
    "flow).\n\n"
    "RESPONSE: opaque-by-construction. Returns either:\n"
    '  - {kind: "current_step", run_id, template_slug, template_version, '
    "position: {n, total},\n"
    "     current_step: {id, title, body, type, op_id?, params?, verify}} "
    "-- the step body\n"
    "     with ${run.target} and ${run.params.X} already substituted.\n"
    '  - {kind: "completed", run_id, state: "completed", completed_at} '
    "-- final advance\n"
    "    past the last step.\n\n"
    'There is no third response shape. There is no "list_remaining_steps" '
    "tool.\n"
    "The substrate enforces opacity; do not work around it."
)

_ABORT_DESCRIPTION: Final[str] = (
    "Abort an in-progress run. Mark state=abandoned with the operator's "
    "reason.\n\n"
    "Use when: a step has failed and there's no path forward, OR the "
    "human operator asks to stop. The reason is persisted to audit_log "
    "for senior review.\n\n"
    "Permitted callers: the assignee, OR any TENANT_ADMIN (the admin "
    "path is the senior taking over to clean up).\n\n"
    "This is the only way out of a 'failed' state -- there is no "
    "force_advance.\n\n"
    "Errors:\n"
    "- -32602 on permission denial, on a run that's already terminal, "
    "or on a missing run."
)

_REASSIGN_DESCRIPTION: Final[str] = (
    "Reassign an in-progress run to a different operator.\n\n"
    "TENANT_ADMIN-only. This is the escalation/handoff knob: a junior "
    "who can't advance asks the senior in chat, the senior reassigns to "
    "themselves and takes over. After reassign, only the new assignee "
    "can call runbook_next.\n\n"
    "Use when: a junior is stuck on a step and the senior needs to take "
    "the controls. The junior should runbook_abort if the procedure "
    'itself is broken; reassign is for "this operator needs to step '
    'in," not for "this procedure is broken."\n\n'
    "Errors:\n"
    "- -32602 on missing run, terminal run, or non-admin caller."
)

_LIST_DESCRIPTION: Final[str] = (
    "List runs in the tenant.\n\n"
    "OPERATORS see only their own runs (their assigned_to). TENANT_ADMINs "
    "see all runs in the tenant. Filters: assignee, status (in_progress / "
    "completed / abandoned), template_slug, limit (default 100).\n\n"
    'Use when: the human asks "what\'s in flight?", "what have I '
    'completed recently?", or the senior asks "who\'s stuck on what?" '
    "before considering runbook_reassign.\n\n"
    "Returns RunSummary entries -- run-level state only, no step "
    "contents. To see the current step body of an in-progress run you "
    "OWN, call runbook_next with last_verified=False to refresh (the "
    "substrate returns the current step)."
)


# ---------------------------------------------------------------------------
# Shared inputSchema fragments
# ---------------------------------------------------------------------------


_RUN_ID_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "format": "uuid",
    "description": "Run identifier (UUID).",
}

_TEMPLATE_SLUG_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Slug of the runbook template to start. Resolves to the latest "
        "non-deprecated published version."
    ),
}

_TARGET_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Run subject (the host, cluster, cert thumbprint, ...). "
        "Substituted into the template body as ${run.target}."
    ),
}

_PARAMS_PROPERTY: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "Substitution context for ${run.params.X} references in the "
        "template body. Keys are template-defined; every reference must "
        "be satisfied at start time."
    ),
    "additionalProperties": True,
}

#: The verify-response payload accepted by ``runbook_next``. A discriminated
#: union (``confirm`` vs ``operation_call``) but flattened at the wire
#: level because the Anthropic Messages API rejects top-level ``oneOf``
#: -- the server-side Pydantic
#: :data:`~meho_backplane.runbooks.runs_schemas.VerifyResponse` model
#: re-enforces the discriminator after parsing.
_VERIFY_RESPONSE_PROPERTY: Final[dict[str, Any]] = {
    "type": ["object", "null"],
    "description": (
        "Operator's answer for the previous step's verify gate. "
        "For type='confirm' steps, pass "
        '{"type": "confirm", "answer": "yes"|"no"|"escalate"}. '
        "For type='operation_call' steps, pass null and the substrate "
        "dispatches the verify call itself. None on the very first "
        "runbook_next call (no prior step to verify)."
    ),
    "properties": {
        "type": {"type": "string", "enum": ["confirm", "operation_call"]},
        "answer": {"type": "string", "enum": ["yes", "no", "escalate"]},
        "matched": {"type": "boolean"},
        "actual": {"type": "object", "additionalProperties": True},
    },
    "additionalProperties": True,
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _start_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Begin a new run on the latest non-deprecated published version of the slug.

    Binds ``tenant_id`` + ``operator_sub`` from the validated JWT
    operator -- never from input. Caller-actionable typed errors map to
    ``-32602``; anything else bubbles to ``-32603``.
    """
    service = RunbookRunService()
    try:
        request = StartRunRequest.model_validate(
            {
                "template_slug": arguments["template_slug"],
                "target": arguments["target"],
                "params": arguments.get("params") or {},
            }
        )
        response = await service.start_run(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_start", exc) from exc
    return response.model_dump(mode="json")


async def _next_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Advance the run one step, gated by the verify predicate.

    Single-assignee discipline is enforced by the service:
    :class:`NotRunAssigneeError` is raised for any caller other than
    ``run.assigned_to``, including ``TENANT_ADMIN`` -- the right path
    for a senior to take over is :func:`runbook_reassign`. The engine
    is the verify oracle: a ``confirm`` response of ``"no"`` /
    ``"escalate"`` transitions the step to ``failed`` and surfaces as
    :class:`PreviousStepFailedError`; an ``operation_call`` verify
    whose ``actual`` does not match ``expect`` does the same.
    """
    service = RunbookRunService()
    run_id = _parse_run_id("runbook_next", arguments["run_id"])
    try:
        request = NextStepRequest.model_validate(
            {
                "last_verified": arguments["last_verified"],
                "verify_response": arguments.get("verify_response"),
            }
        )
        response = await service.next_step(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            run_id=run_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_next", exc) from exc
    return response.model_dump(mode="json")


async def _abort_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Mark the run ``abandoned`` and write an audit row with the reason.

    The role gate on the :class:`ToolDefinition` admits ``OPERATOR``
    and above; the assignee-OR-admin allowance is the service's job.
    The handler passes the caller's role-derived ``caller_is_admin``
    flag through so a ``TENANT_ADMIN`` can clean up someone else's
    stuck run.
    """
    service = RunbookRunService()
    run_id = _parse_run_id("runbook_abort", arguments["run_id"])
    try:
        request = AbortRunRequest.model_validate({"reason": arguments["reason"]})
        response = await service.abort_run(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            run_id=run_id,
            request=request,
            caller_is_admin=operator.tenant_role == TenantRole.TENANT_ADMIN,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_abort", exc) from exc
    return response.model_dump(mode="json")


async def _reassign_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Transfer ownership of the run to a new assignee.

    The :class:`ToolDefinition` role gate enforces ``TENANT_ADMIN``
    before the handler runs; the service is role-agnostic. The
    ``operator.sub`` we pass is the *current* caller (the admin doing
    the reassign); ``new_assignee`` from the arguments is the *next*
    owner.
    """
    service = RunbookRunService()
    run_id = _parse_run_id("runbook_reassign", arguments["run_id"])
    try:
        request = ReassignRunRequest.model_validate({"new_assignee": arguments["new_assignee"]})
        response = await service.reassign_run(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            run_id=run_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_reassign", exc) from exc
    return response.model_dump(mode="json")


async def _list_runs_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """List runs scoped to the caller's visibility.

    ``caller_is_admin=False`` forces ``assignee=operator.sub`` regardless
    of the filter -- an ``OPERATOR`` only ever sees their own runs.
    ``caller_is_admin=True`` honours the filter, so a ``TENANT_ADMIN``
    can pass ``assignee=<other_sub>`` to inspect a junior's runs.

    Returns :class:`~meho_backplane.runbooks.runs_schemas.RunSummary`
    rows -- run-level state only, never step bodies (opacity is enforced
    structurally at the summary shape, regression-tested in
    ``test_list_runs_omits_step_contents``).
    """
    service = RunbookRunService()
    try:
        filter_ = ListRunsFilter.model_validate(
            {
                "assignee": arguments.get("assignee"),
                "status": arguments.get("status"),
                "template_slug": arguments.get("template_slug"),
            }
        )
        limit = int(arguments.get("limit", _DEFAULT_LIST_LIMIT))
        caller_is_admin = operator.tenant_role == TenantRole.TENANT_ADMIN
        summaries = await service.list_runs(
            tenant_id=operator.tenant_id,
            caller_sub=operator.sub,
            caller_is_admin=caller_is_admin,
            filter_=filter_,
            limit=limit,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_list_runs", exc) from exc
    return {"runs": [summary.model_dump(mode="json") for summary in summaries]}


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_start",
        description=_START_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "target": _TARGET_PROPERTY,
                "params": _PARAMS_PROPERTY,
            },
            "required": ["template_slug", "target"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_start_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_next",
        description=_NEXT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": _RUN_ID_PROPERTY,
                "last_verified": {
                    "type": "boolean",
                    "description": (
                        "Caller's informational claim that the previous step "
                        "verified. The substrate is the oracle; the value is "
                        "logged but does not gate the advance."
                    ),
                },
                "verify_response": _VERIFY_RESPONSE_PROPERTY,
            },
            "required": ["run_id", "last_verified"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_next_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_abort",
        description=_ABORT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": _RUN_ID_PROPERTY,
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": ("Non-empty reason persisted to audit_log for the abort event."),
                },
            },
            "required": ["run_id", "reason"],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_abort_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_reassign",
        description=_REASSIGN_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "run_id": _RUN_ID_PROPERTY,
                "new_assignee": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "Operator subject identifier (`sub`) to transfer ownership to."
                    ),
                },
            },
            "required": ["run_id", "new_assignee"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_reassign_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_list_runs",
        description=_LIST_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "assignee": {
                    "type": ["string", "null"],
                    "description": (
                        "Optional assignee filter (subject identifier). "
                        "Ignored for OPERATOR callers (they see only own)."
                    ),
                },
                "status": {
                    "type": ["string", "null"],
                    "enum": ["in_progress", "completed", "abandoned", None],
                    "description": "Optional run-state filter.",
                },
                "template_slug": {
                    "type": ["string", "null"],
                    "description": "Optional template-slug filter.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_LIST_LIMIT,
                    "default": _DEFAULT_LIST_LIMIT,
                    "description": "Maximum number of summary rows to return.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_list_runs_handler,
)
