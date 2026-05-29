# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``runbook_*_template`` MCP tools — template lifecycle (G12.2-T4).

Six tool registrations that surface
:class:`~meho_backplane.runbooks.service.RunbookTemplateService` on the
MCP transport. The matching REST routes are owned by the sibling Task
#1297; both transports converge on the same service, so the versioning
algebra (edit-draft vs fork-from-published), the status state machine,
and the ``in_flight_run_count`` query all live in one place (T2, #1296).

Why the descriptions are long
=============================

(Same call-out as :mod:`meho_backplane.mcp.tools.knowledge` — the agent
UX lives in the descriptions.) The descriptions below follow the
AI-engineering anchor: name *what* the tool does, *when* to use it,
*when NOT* to use it, and how the output composes with the other
runbook tools.

The :data:`_EDIT_DESCRIPTION` text teaches the multi-session drafting
pattern explicitly because there is nowhere else for the agent to learn
it short of reading ``docs/runbooks/authoring.md`` (which G12.2-T5 ships
— the cross-reference below is intentional and resolves once T5 lands).
The description IS the contract for "how to use this tool well."

RBAC, audit, error mapping
===========================

Five of the six tools are ``TENANT_ADMIN``-only (authoring + status
flips); ``runbook_list_templates`` is ``OPERATOR``-readable (summaries,
no step bodies). ``runbook_show_template`` stays ``TENANT_ADMIN``-only
because the template-side read is the authoring / review surface; step
opacity at execution time is enforced by G12.3 at the *run* surface (the
post-completion read exception G12.3 layers on top is out of scope here).
The role gate on each :class:`ToolDefinition` is enforced by the
dispatcher *before* the handler runs, so an ``OPERATOR`` calling a
``TENANT_ADMIN`` tool is refused with JSON-RPC ``-32602`` and never
reaches the handler.

Same dispatcher mechanism as the kb tools — one ``audit_log`` row + one
broadcast event per ``tools/call``, with ``op_class`` (``"read"`` for
list / show, ``"write"`` for the rest) flowing into
:func:`~meho_backplane.broadcast.classify.classify_op`.

The operator-actionable service exceptions (:data:`_INVALID_PARAMS_ERRORS`)
plus a Pydantic ``ValidationError`` on the request model map to
``-32602`` via :class:`McpInvalidParamsError`; anything else bubbles to
the dispatcher's generic ``-32603``.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.mcp.registry import ToolDefinition, register_mcp_tool
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    DraftTemplateRequest,
    EditTemplateRequest,
    ListTemplatesFilter,
    PublishTemplateRequest,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    RunbookTemplateService,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)

__all__: list[str] = []


#: Op-class strings keep parity with the
#: :mod:`meho_backplane.broadcast.classify` taxonomy. Authoring + status
#: flips are ``write``; list + show are ``read``.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"

#: The caller-actionable service exceptions that map to JSON-RPC
#: ``-32602``. Bad input or a missing / wrong-state entity is the
#: client's problem, not a server fault — surfacing it as
#: ``INVALID_PARAMS`` (rather than letting it bubble to the generic
#: ``-32603``) gives the agent a self-correcting signal.
#: Left unannotated so mypy infers the precise concrete-member tuple
#: (rather than ``tuple[type[Exception], ...]``) — the variadic form is
#: rejected as an ``except`` argument.
_INVALID_PARAMS_ERRORS = (
    DuplicateDraftError,
    TemplateNotFoundError,
    TemplateNotDraftError,
    TemplateNotPublishedError,
    InvalidKbSlugError,
)

#: Default + maximum row cap for ``runbook_list_templates``. Matches the
#: :meth:`RunbookTemplateService.list_templates` default (100).
_DEFAULT_LIST_LIMIT: Final[int] = 100
_MAX_LIST_LIMIT: Final[int] = 500


def _to_invalid_params(tool: str, exc: Exception) -> McpInvalidParamsError:
    """Wrap a caller-actionable service exception as :class:`McpInvalidParamsError`.

    Centralised so every handler maps the typed-exception set in
    :data:`_INVALID_PARAMS_ERRORS` to the spec-correct ``-32602`` with a
    consistent ``"<tool>: <detail>"`` message shape.
    """
    return McpInvalidParamsError(f"{tool}: {exc}")


# ---------------------------------------------------------------------------
# Tool descriptions (the load-bearing UX)
# ---------------------------------------------------------------------------


_DRAFT_DESCRIPTION: Final[str] = (
    "Create the first draft of a new runbook template. version=1, "
    "status=draft.\n\n"
    "Use when: a senior is about to walk you through a procedure (cert "
    "rotation, host onboarding, vault unseal-after-restart, anything that "
    "was tribal knowledge until now) and wants it captured as a "
    "governance-graded artifact.\n\n"
    "If a draft already exists for the slug, this fails with -32602. Use "
    "`runbook_edit_template` to mutate the existing draft.\n\n"
    "Requires TENANT_ADMIN role."
)

_EDIT_DESCRIPTION: Final[str] = (
    "Edit a runbook template. The behavior depends on the template's "
    "current status:\n\n"
    "- If a draft exists for this slug, edit it IN PLACE. No version bump.\n"
    "- If only published/deprecated versions exist, FORK to a new draft at "
    "version=max+1.\n"
    "  The response carries `forked_from` so you can see how many in-flight "
    "runs are\n"
    "  still pinned to the version you're forking from.\n\n"
    "MULTI-SESSION DRAFTING PATTERN: a senior+agent run is rarely a single "
    "session.\n"
    "Authoring a real cert-rotation runbook means walking through a "
    "procedure across\n"
    "days. The pattern is:\n"
    "  1. Start a draft with `runbook_draft_template(slug, minimal_body)`.\n"
    "  2. As the procedure surfaces (the senior walks you through it, you "
    "capture\n"
    "     bash + verify steps), call `runbook_edit_template(slug, "
    "updated_body)`\n"
    "     periodically to persist progress.\n"
    "  3. Coming back in a NEW session, call `runbook_show_template(slug)` "
    "to read\n"
    "     the current draft, then resume appending via further "
    "`runbook_edit_template`\n"
    "     calls.\n"
    "  4. Once the senior signs off, `runbook_publish_template(slug, "
    "version)` flips\n"
    "     status to published.\n\n"
    "Drafts are mutable across sessions. Published versions are pinned for "
    "in-flight\n"
    "runs and forked-on-edit.\n\n"
    "Requires TENANT_ADMIN role.\n\n"
    "When NOT to use: editing while a junior is mid-run against the same "
    "slug. Forking\n"
    "on edit means new starts pick up the new draft once published, but "
    "mid-run\n"
    "operators stay pinned to their version. If you want to change the "
    "in-flight\n"
    "procedure, use `runbook_abort` + start over against the new version "
    "instead.\n\n"
    "The `forked_from.in_flight_run_count` field tells you how many "
    "operators are\n"
    "still mid-procedure on the version you're editing.\n\n"
    "For the full authoring walkthrough see docs/runbooks/authoring.md."
)

_PUBLISH_DESCRIPTION: Final[str] = (
    "Flip a draft template to published. Idempotent on already-published "
    "versions.\n\n"
    "After publish, the template is the latest start target for "
    "`runbook_start`.\n"
    "Previous published versions stay addressable for in-flight runs (which "
    "are\n"
    "pinned at start time) and for `runbook_show_template`.\n\n"
    "Use when: the senior has signed off on a draft and wants juniors to be "
    "able to\n"
    "start runs against it.\n\n"
    "Requires TENANT_ADMIN role.\n\n"
    "Errors:\n"
    "- -32602 if the template is missing or already deprecated."
)

_DEPRECATE_DESCRIPTION: Final[str] = (
    "Mark a published version as deprecated. In-flight runs continue to "
    "advance\n"
    "(they're pinned), but new `runbook_start` calls against this version "
    "are refused.\n"
    "`runbook_start` falls back to the latest non-deprecated published "
    "version of\n"
    "the slug.\n\n"
    "Use when: a procedure is no longer current (cert validity period "
    "changed, a\n"
    "backend was upgraded) and you want to stop new operators starting "
    "against it.\n\n"
    "Requires TENANT_ADMIN role."
)

_LIST_DESCRIPTION: Final[str] = (
    "List runbook templates in the operator's tenant. Returns slugs + "
    "titles + status\n"
    "+ target_kind + edited_at. Does NOT return step bodies — use\n"
    "`runbook_show_template` (TENANT_ADMIN-only) to read full content.\n\n"
    "Operators see this surface to discover available procedures before\n"
    "`runbook_start`. TENANT_ADMINs see the same to audit what's in "
    "flight.\n\n"
    "Filters: status (draft/published/deprecated), target_kind. Limit "
    "default 100."
)

_SHOW_DESCRIPTION: Final[str] = (
    "Read the full body of a runbook template, including step contents.\n\n"
    "ROLE GATE: TENANT_ADMIN-only.\n\n"
    "The TENANT_ADMIN gate is the authoring/review surface. **For operators "
    "executing\n"
    "a runbook, step opacity lives on the run surface — `runbook_next` "
    "returns only\n"
    "the current step at run time, not the whole template.** An operator "
    "calling\n"
    "`runbook_show_template` directly is denied (-32602).\n\n"
    "POST-COMPLETION EXCEPTION (G12.3): once an operator has a completed or "
    "abandoned\n"
    "run against (slug, version), reading the template they ran is allowed "
    "for\n"
    "post-mortem / learning. While state=in_progress the 403 still holds — "
    "opacity\n"
    "floor is real.\n\n"
    "Use when: authoring (editing your own drafts), reviewing a peer's "
    "draft before\n"
    "publish, or learning from a runbook you've already completed. See "
    "docs/runbooks/authoring.md for the authoring workflow.\n\n"
    "The `version` parameter is optional — when omitted, returns the latest "
    "version."
)


# ---------------------------------------------------------------------------
# Shared inputSchema fragments
# ---------------------------------------------------------------------------


_SLUG_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Template identifier in kebab-case (the same slug shape as kb "
        "entries): must start with a lowercase letter and contain only "
        "lowercase letters, digits, hyphens, or dots. Example: "
        "'vcenter-9.0-cert-rotation'."
    ),
}

#: The author-facing template body shape. Mirrors
#: :class:`~meho_backplane.runbooks.schemas.RunbookTemplateBody`; the
#: discriminated-union step shapes are validated by the request model in
#: the handler (the wire schema keeps ``steps`` loosely typed as an array
#: of objects so the Anthropic Messages API does not choke on a nested
#: ``oneOf``/discriminator at tool-publish time — the strict shape is
#: re-enforced server-side by the Pydantic request model).
_BODY_PROPERTY: Final[dict[str, Any]] = {
    "type": "object",
    "description": (
        "The template body: title, description, optional target_kind, and "
        "an ordered list of steps. Each step is either an "
        "'operation_call' step (the agent dispatches it via an operation "
        "id) or a 'manual' step (the operator performs it off-MEHO), and "
        "carries a 'verify' gate ('confirm' or 'operation_call'). "
        "Substitutions are limited to ${run.target} and ${run.params.X}."
    ),
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "target_kind": {"type": ["string", "null"]},
        "steps": {
            "type": "array",
            "items": {"type": "object"},
        },
    },
    "required": ["title", "description", "steps"],
    "additionalProperties": True,
}

_VERSION_PROPERTY: Final[dict[str, Any]] = {
    "type": "integer",
    "minimum": 1,
    "description": "The template version to act on.",
}


# ---------------------------------------------------------------------------
# Handler implementations
# ---------------------------------------------------------------------------


async def _draft_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Create the first draft of a new template for the operator's tenant.

    Binds ``tenant_id`` + ``operator_sub`` from the validated JWT
    operator — never from input. Maps
    :class:`DuplicateDraftError` / :class:`InvalidKbSlugError` to
    ``-32602``.
    """
    service = RunbookTemplateService()
    try:
        request = DraftTemplateRequest.model_validate(
            {"slug": arguments["slug"], "body": arguments["body"]}
        )
        response = await service.create_draft(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_draft_template", exc) from exc
    return response.model_dump(mode="json")


async def _edit_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Edit a draft in place, or fork a new draft from the latest published.

    The service picks the path (the caller does not). On the fork path the
    response carries ``forked_from`` (source version + ``in_flight_run_count``);
    on the in-place path it is ``None``.
    """
    service = RunbookTemplateService()
    try:
        request = EditTemplateRequest.model_validate(
            {"slug": arguments["slug"], "body": arguments["body"]}
        )
        response = await service.update_or_fork(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_edit_template", exc) from exc
    return response.model_dump(mode="json")


async def _publish_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Promote ``(slug, version)`` from draft to published. Idempotent."""
    service = RunbookTemplateService()
    try:
        request = PublishTemplateRequest.model_validate(
            {"slug": arguments["slug"], "version": arguments["version"]}
        )
        response = await service.publish(
            tenant_id=operator.tenant_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_publish_template", exc) from exc
    return response.model_dump(mode="json")


async def _deprecate_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Retire ``(slug, version)`` from published to deprecated. Idempotent."""
    service = RunbookTemplateService()
    try:
        request = DeprecateTemplateRequest.model_validate(
            {"slug": arguments["slug"], "version": arguments["version"]}
        )
        response = await service.deprecate(
            tenant_id=operator.tenant_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_deprecate_template", exc) from exc
    return response.model_dump(mode="json")


async def _list_templates_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """List the latest version of each slug in the operator's tenant.

    Returns operator-readable summaries (no step bodies). Optional
    ``status`` / ``target_kind`` filters narrow the rows considered before
    the latest-per-slug projection.
    """
    service = RunbookTemplateService()
    try:
        filter_ = ListTemplatesFilter.model_validate(
            {
                "status": arguments.get("status"),
                "target_kind": arguments.get("target_kind"),
            }
        )
        summaries = await service.list_templates(
            tenant_id=operator.tenant_id,
            filter_=filter_,
            limit=int(arguments.get("limit", _DEFAULT_LIST_LIMIT)),
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("runbook_list_templates", exc) from exc
    return {"templates": [summary.model_dump(mode="json") for summary in summaries]}


async def _show_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the full template body for ``(slug, version?)``.

    TENANT_ADMIN-only — the dispatcher refuses an operator before this
    handler runs (the opacity floor lives on the run surface, G12.3).
    Resolves the latest version when ``version`` is omitted.
    """
    service = RunbookTemplateService()
    try:
        version = arguments.get("version")
        response = await service.show_template(
            tenant_id=operator.tenant_id,
            slug=arguments["slug"],
            version=int(version) if version is not None else None,
        )
    except _INVALID_PARAMS_ERRORS as exc:
        raise _to_invalid_params("runbook_show_template", exc) from exc
    return response.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_draft_template",
        description=_DRAFT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "slug": _SLUG_PROPERTY,
                "body": _BODY_PROPERTY,
            },
            "required": ["slug", "body"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_draft_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_edit_template",
        description=_EDIT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "slug": _SLUG_PROPERTY,
                "body": _BODY_PROPERTY,
            },
            "required": ["slug", "body"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_publish_template",
        description=_PUBLISH_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "slug": _SLUG_PROPERTY,
                "version": _VERSION_PROPERTY,
            },
            "required": ["slug", "version"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_publish_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_deprecate_template",
        description=_DEPRECATE_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "slug": _SLUG_PROPERTY,
                "version": _VERSION_PROPERTY,
            },
            "required": ["slug", "version"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_deprecate_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_list_templates",
        description=_LIST_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "status": {
                    "type": ["string", "null"],
                    "enum": ["draft", "published", "deprecated", None],
                    "description": "Optional status filter.",
                },
                "target_kind": {
                    "type": ["string", "null"],
                    "description": "Optional target_kind filter.",
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
    handler=_list_templates_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="runbook_show_template",
        description=_SHOW_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "slug": _SLUG_PROPERTY,
                "version": {
                    **_VERSION_PROPERTY,
                    "description": (
                        "Optional template version. Omit to return the latest version."
                    ),
                },
            },
            "required": ["slug"],
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_READ,
    ),
    handler=_show_template_handler,
)
