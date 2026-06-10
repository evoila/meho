# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""``meho.runbook.*`` template-lifecycle MCP tools (G12.2-T4).

Six tool registrations that surface
:class:`~meho_backplane.runbooks.service.RunbookTemplateService` on the
MCP transport. The matching REST routes are owned by the sibling Task
#1297; both transports converge on the same service, so the versioning
algebra (edit-draft vs fork-from-published), the status state machine,
and the ``in_flight_run_count`` query all live in one place (T2, #1296).

Naming + field canonicalisation (#1612)
=======================================

The canonical tool names are dotted — ``meho.runbook.draft_template``,
``meho.runbook.show_template``, … — matching the ``meho.<noun>.<verb>``
grammar every other multi-verb family on the surface uses. The original
flat ``runbook_*`` names remain registered as deprecated aliases (same
handler object, same schema) for one release and are removed in
v0.14.0; calling one emits the dispatcher's ``mcp_tool_name_deprecated``
warning.

The template identifier is canonically ``template_slug`` on every wire
input (matching ``meho.runbook.start`` / ``meho.runbook.list_runs`` on
the run side), with the pre-#1612 ``slug`` accepted as a deprecated
input alias for the same window via :func:`_resolve_template_slug`.
Responses mirror the model's ``slug`` key as ``template_slug`` (see
:func:`_mirror_template_slug`) so an id read from any template verb
round-trips verbatim into ``meho.runbook.start`` with no field rename.

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

Four of the six tools are ``TENANT_ADMIN``-only (authoring + status
flips). ``meho.runbook.list_templates`` is ``OPERATOR``-readable
(summaries, no step bodies). ``meho.runbook.show_template`` admits an
``OPERATOR`` at the
dispatcher gate but applies a *run-state-conditional* carve-out inside
the handler (G12.3-T4 / #1309): a ``TENANT_ADMIN`` is a pass-through, an
``OPERATOR`` is granted the read only when
:meth:`~meho_backplane.runbooks.run_service.RunbookRunService.can_show_template_post_completion`
returns ``True`` -- i.e. the operator has a ``completed`` or ``abandoned``
run against the resolved ``(slug, version)``. Otherwise the handler
refuses with :class:`McpInvalidParamsError` carrying the
``opacity_floor`` reason -- the JSON-RPC ``-32602`` shape an ``OPERATOR``
already gets on every other denial path, but with a body keyword that
distinguishes "opacity floor held" from "schema invalid".

The role gate on each :class:`ToolDefinition` is enforced by the
dispatcher *before* the handler runs, so an ``OPERATOR`` calling a
``TENANT_ADMIN`` tool is refused with JSON-RPC ``-32602`` and never
reaches the handler. The handler-internal opacity-floor check is
strictly *after* the dispatcher gate fires (and is irrelevant for
admins).

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

import structlog
from pydantic import ValidationError

from meho_backplane.auth.operator import Operator, TenantRole
from meho_backplane.kb.schemas import InvalidKbSlugError
from meho_backplane.mcp.registry import (
    ToolDefinition,
    register_deprecated_mcp_tool_alias,
    register_mcp_tool,
)
from meho_backplane.mcp.server import McpInvalidParamsError
from meho_backplane.runbooks.run_service import RunbookRunService
from meho_backplane.runbooks.schemas import (
    DeprecateTemplateRequest,
    DraftTemplateRequest,
    EditTemplateRequest,
    ListTemplatesFilter,
    PublishTemplateRequest,
    ShowTemplateResponse,
)
from meho_backplane.runbooks.service import (
    DuplicateDraftError,
    RunbookTemplateService,
    TemplateNotDraftError,
    TemplateNotFoundError,
    TemplateNotPublishedError,
)

__all__: list[str] = []

_log = structlog.get_logger(__name__)


#: Op-class strings keep parity with the
#: :mod:`meho_backplane.broadcast.classify` taxonomy. Authoring + status
#: flips are ``write``; list + show are ``read``.
_OP_CLASS_READ: Final[str] = "read"
_OP_CLASS_WRITE: Final[str] = "write"

#: Release that drops the deprecated flat ``runbook_*`` tool-name
#: aliases and the ``slug`` input-field alias (#1612). One release after
#: the canonicalisation ships (announced in the CHANGELOG ``Deprecated``
#: section), mirroring the v0.6.x ``content``→``body`` one-cycle shim.
_ALIAS_REMOVAL_VERSION: Final[str] = "0.14.0"

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

#: Default + maximum row cap for ``meho.runbook.list_templates``. Matches the
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


def _resolve_template_slug(tool: str, arguments: dict[str, Any]) -> str:
    """Resolve the template id from ``template_slug`` (canonical) or ``slug``.

    One-cycle deprecation shim for the #1612 ``slug`` → ``template_slug``
    field unification, mirroring the v0.6.x ``content`` → ``body`` shim
    on ``add_to_memory``: exactly one of the two names must be supplied
    (both → ``-32602``), and the deprecated name emits a structured
    ``runbook_template_slug_field_deprecated`` warning so operators can
    watch consumers migrate before the alias is dropped in
    v0.14.0 (:data:`_ALIAS_REMOVAL_VERSION`).

    The dispatcher's JSON-Schema gate (:data:`_TEMPLATE_SLUG_ANYOF`)
    guarantees at least one name is present; the neither-supplied branch
    is the fail-closed answer to a schema regression, not a real
    run-time path.
    """
    has_canonical = "template_slug" in arguments
    has_alias = "slug" in arguments
    if has_canonical and has_alias:
        raise McpInvalidParamsError(
            f"{tool}: pass either `template_slug` (canonical) or `slug` "
            "(deprecated alias), not both",
        )
    if has_alias:
        _log.warning(
            "runbook_template_slug_field_deprecated",
            tool=tool,
            replacement="template_slug",
            removal_version=_ALIAS_REMOVAL_VERSION,
        )
        return str(arguments["slug"])
    if not has_canonical:
        raise McpInvalidParamsError(
            f"{tool}: one of `template_slug` or `slug` must be supplied",
        )
    return str(arguments["template_slug"])


def _mirror_template_slug(payload: dict[str, Any]) -> dict[str, Any]:
    """Mirror a top-level ``slug`` response key as ``template_slug``.

    The shared Pydantic response models keep their ``slug`` field (the
    REST surface is out of #1612's scope), so the MCP handlers mirror it
    at the wire boundary: every template-verb response carries
    ``template_slug`` (canonical, what ``meho.runbook.start`` accepts
    verbatim) *and* ``slug`` (kept for the same one-release window as
    the input alias, dropped with it in v0.14.0). Top-level only — the
    nested ``forked_from.slug`` names the fork *source* and is not a
    round-trip input.
    """
    if "slug" in payload:
        payload["template_slug"] = payload["slug"]
    return payload


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
    "`meho.runbook.edit_template` to mutate the existing draft.\n\n"
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
    "  1. Start a draft with `meho.runbook.draft_template(template_slug, "
    "minimal_body)`.\n"
    "  2. As the procedure surfaces (the senior walks you through it, you "
    "capture\n"
    "     bash + verify steps), call "
    "`meho.runbook.edit_template(template_slug, updated_body)`\n"
    "     periodically to persist progress.\n"
    "  3. Coming back in a NEW session, call "
    "`meho.runbook.show_template(template_slug)` to read\n"
    "     the current draft, then resume appending via further "
    "`meho.runbook.edit_template`\n"
    "     calls.\n"
    "  4. Once the senior signs off, "
    "`meho.runbook.publish_template(template_slug, version)` flips\n"
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
    "procedure, use `meho.runbook.abort` + start over against the new "
    "version instead.\n\n"
    "The `forked_from.in_flight_run_count` field tells you how many "
    "operators are\n"
    "still mid-procedure on the version you're editing.\n\n"
    "For the full authoring walkthrough see docs/runbooks/authoring.md."
)

_PUBLISH_DESCRIPTION: Final[str] = (
    "Flip a draft template to published. Idempotent on already-published "
    "versions.\n\n"
    "After publish, the template is the latest start target for "
    "`meho.runbook.start`.\n"
    "Previous published versions stay addressable for in-flight runs (which "
    "are\n"
    "pinned at start time) and for `meho.runbook.show_template`.\n\n"
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
    "(they're pinned), but new `meho.runbook.start` calls against this "
    "version are refused.\n"
    "`meho.runbook.start` falls back to the latest non-deprecated published "
    "version of\n"
    "the slug.\n\n"
    "Use when: a procedure is no longer current (cert validity period "
    "changed, a\n"
    "backend was upgraded) and you want to stop new operators starting "
    "against it.\n\n"
    "Requires TENANT_ADMIN role."
)

_LIST_DESCRIPTION: Final[str] = (
    "List runbook templates in the operator's tenant. Returns "
    "template_slugs + titles + status\n"
    "+ target_kind + edited_at. Does NOT return step bodies — use\n"
    "`meho.runbook.show_template` (TENANT_ADMIN-only) to read full "
    "content.\n\n"
    "Operators see this surface to discover available procedures before\n"
    "`meho.runbook.start` — each summary's `template_slug` is accepted "
    "verbatim by `meho.runbook.start`. TENANT_ADMINs see the same to "
    "audit what's in flight.\n\n"
    "Filters: status (draft/published/deprecated), target_kind. Limit "
    "default 100."
)

_SHOW_DESCRIPTION: Final[str] = (
    "Read the full body of a runbook template, including step contents.\n\n"
    "ROLE GATE: TENANT_ADMIN unconditionally; OPERATOR only with the "
    "post-completion\n"
    "carve-out below.\n\n"
    "For TENANT_ADMIN this is the authoring/review surface — admins read "
    "freely.\n"
    "**For operators executing a runbook, step opacity lives on the run "
    "surface — \n"
    "`meho.runbook.next` returns only the current step at run time, not the "
    "whole "
    "template.** An operator calling `meho.runbook.show_template` directly "
    "while a run\n"
    "is in flight is denied (-32602, opacity_floor).\n\n"
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
    "The `version` parameter is optional — when omitted, the latest tenant "
    "version is resolved first; an operator must hold a completed or "
    "abandoned run against that specific resolved version to be granted the "
    "read.\n\n"
    "The response's `template_slug` is accepted verbatim by "
    "`meho.runbook.start` — no field rename needed."
)


# ---------------------------------------------------------------------------
# Shared inputSchema fragments
# ---------------------------------------------------------------------------


_TEMPLATE_SLUG_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "Template identifier in kebab-case (the same slug shape as kb "
        "entries): must start with a lowercase letter and contain only "
        "lowercase letters, digits, hyphens, or dots. Example: "
        "'vcenter-9.0-cert-rotation'. REQUIRED (unless the deprecated "
        "`slug` alias is supplied instead). Canonical field name across "
        "all 11 runbook tools (#1612) — the same name "
        "`meho.runbook.start` takes and every template response returns."
    ),
}

#: Deprecated ``slug`` input alias kept for the pre-#1612 wire shape.
#: Same constraints as :data:`_TEMPLATE_SLUG_PROPERTY`; the handler
#: enforces the XOR and emits the migration warning (see
#: :func:`_resolve_template_slug`).
_SLUG_ALIAS_PROPERTY: Final[dict[str, Any]] = {
    "type": "string",
    "minLength": 1,
    "description": (
        "DEPRECATED alias for `template_slug` (pre-#1612 wire shape); "
        f"removed in v{_ALIAS_REMOVAL_VERSION}. Accepted for backward "
        "compatibility; new callers MUST use `template_slug`. Mutually "
        "exclusive with `template_slug`; passing both rejects with "
        "-32602. Supplying `slug` emits a structured "
        "`runbook_template_slug_field_deprecated` warning log."
    ),
    "deprecated": True,
}

#: Either field name satisfies the "template id required" constraint;
#: the handler enforces the XOR. Top-level ``anyOf`` is stripped from
#: the *wire* copy of the schema by ``_wire_safe_input_schema`` (the
#: Anthropic Messages API rejects top-level combinators), so the wire
#: ``required`` omits the template id and the property descriptions
#: carry the contract; server-side ``jsonschema.validate`` still
#: enforces it before the handler runs. Same shape as the
#: ``approval_request_id``/``id`` and ``body``/``content`` shims.
_TEMPLATE_SLUG_ANYOF: Final[list[dict[str, Any]]] = [
    {"required": ["template_slug"]},
    {"required": ["slug"]},
]

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
    slug = _resolve_template_slug("meho.runbook.draft_template", arguments)
    try:
        request = DraftTemplateRequest.model_validate({"slug": slug, "body": arguments["body"]})
        response = await service.create_draft(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("meho.runbook.draft_template", exc) from exc
    return _mirror_template_slug(response.model_dump(mode="json"))


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
    slug = _resolve_template_slug("meho.runbook.edit_template", arguments)
    try:
        request = EditTemplateRequest.model_validate({"slug": slug, "body": arguments["body"]})
        response = await service.update_or_fork(
            tenant_id=operator.tenant_id,
            operator_sub=operator.sub,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("meho.runbook.edit_template", exc) from exc
    return _mirror_template_slug(response.model_dump(mode="json"))


async def _publish_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Promote ``(slug, version)`` from draft to published. Idempotent."""
    service = RunbookTemplateService()
    slug = _resolve_template_slug("meho.runbook.publish_template", arguments)
    try:
        request = PublishTemplateRequest.model_validate(
            {"slug": slug, "version": arguments["version"]}
        )
        response = await service.publish(
            tenant_id=operator.tenant_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("meho.runbook.publish_template", exc) from exc
    return _mirror_template_slug(response.model_dump(mode="json"))


async def _deprecate_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Retire ``(slug, version)`` from published to deprecated. Idempotent."""
    service = RunbookTemplateService()
    slug = _resolve_template_slug("meho.runbook.deprecate_template", arguments)
    try:
        request = DeprecateTemplateRequest.model_validate(
            {"slug": slug, "version": arguments["version"]}
        )
        response = await service.deprecate(
            tenant_id=operator.tenant_id,
            request=request,
        )
    except (ValidationError, *_INVALID_PARAMS_ERRORS) as exc:
        raise _to_invalid_params("meho.runbook.deprecate_template", exc) from exc
    return _mirror_template_slug(response.model_dump(mode="json"))


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
        raise _to_invalid_params("meho.runbook.list_templates", exc) from exc
    return {
        "templates": [
            _mirror_template_slug(summary.model_dump(mode="json")) for summary in summaries
        ]
    }


async def _show_template_handler(
    operator: Operator,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Return the full template body for ``(slug, version?)``.

    Role floor: ``OPERATOR``. ``TENANT_ADMIN`` is a pass-through (the
    authoring / review surface). An ``OPERATOR`` is granted the read only
    when
    :meth:`~meho_backplane.runbooks.run_service.RunbookRunService.can_show_template_post_completion`
    returns ``True`` for the resolved ``(slug, version)`` -- i.e. the
    operator has a ``completed`` or ``abandoned`` run against that pinned
    version (post-mortem / learning carve-out, G12.3-T4 #1309). Anything
    else (no run, ``in_progress`` only, wrong version, slug missing) is
    refused with :class:`McpInvalidParamsError` carrying the
    ``opacity_floor`` reason -- the same JSON-RPC ``-32602`` shape any
    other denial uses, with the body keyword that distinguishes "opacity
    floor held" from "schema invalid".

    Resolves the latest version when ``version`` is omitted; the operator
    branch resolves the latest *first* so the authorization check uses the
    same version the response would return (see
    :func:`_show_template_operator_path`).
    """
    template_service = RunbookTemplateService()
    slug = _resolve_template_slug("meho.runbook.show_template", arguments)
    raw_version = arguments.get("version")
    requested_version = int(raw_version) if raw_version is not None else None

    if operator.tenant_role == TenantRole.TENANT_ADMIN:
        try:
            response = await template_service.show_template(
                tenant_id=operator.tenant_id, slug=slug, version=requested_version
            )
        except _INVALID_PARAMS_ERRORS as exc:
            raise _to_invalid_params("meho.runbook.show_template", exc) from exc
        return _mirror_template_slug(response.model_dump(mode="json"))

    response = await _show_template_operator_path(
        template_service=template_service,
        run_service=RunbookRunService(),
        operator=operator,
        slug=slug,
        requested_version=requested_version,
    )
    return _mirror_template_slug(response.model_dump(mode="json"))


async def _show_template_operator_path(
    *,
    template_service: RunbookTemplateService,
    run_service: RunbookRunService,
    operator: Operator,
    slug: str,
    requested_version: int | None,
) -> ShowTemplateResponse:
    """Apply the run-state-conditional carve-out for OPERATOR callers.

    Differential-timing posture: when ``requested_version`` is ``None``,
    the latest version is resolved **first** so the authorization check
    runs against the same version the response would return. Resolution
    failure on a slug the operator has no run against still surfaces as
    ``opacity_floor`` rather than a not-found channel -- a slug-existence
    channel an operator cannot read into would otherwise leak template
    presence to enumerators.
    """
    if requested_version is None:
        try:
            response = await template_service.show_template(
                tenant_id=operator.tenant_id, slug=slug, version=None
            )
        except TemplateNotFoundError as exc:
            raise _opacity_floor_denial() from exc
        if await run_service.can_show_template_post_completion(
            operator.tenant_id, operator.sub, slug, response.version
        ):
            return response
        raise _opacity_floor_denial()

    if not await run_service.can_show_template_post_completion(
        operator.tenant_id, operator.sub, slug, requested_version
    ):
        raise _opacity_floor_denial()
    try:
        return await template_service.show_template(
            tenant_id=operator.tenant_id, slug=slug, version=requested_version
        )
    except TemplateNotFoundError as exc:
        raise _opacity_floor_denial() from exc


def _opacity_floor_denial() -> McpInvalidParamsError:
    """Construct the canonical opacity-floor refusal.

    Centralised so every operator-path denial uses the same message --
    anti-enumeration relies on the response being identical regardless of
    which leg of the predicate failed (slug missing, version missing, no
    completed run). The message names the canonical tool even when the
    call arrived via the deprecated ``runbook_show_template`` alias --
    deriving it from the called name would cost the single-message
    property nothing, but pinning it keeps the denial byte-identical
    across both names for the deprecation window.
    """
    return McpInvalidParamsError("meho.runbook.show_template: opacity_floor")


# ---------------------------------------------------------------------------
# Tool registrations
# ---------------------------------------------------------------------------


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.runbook.draft_template",
        description=_DRAFT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "slug": _SLUG_ALIAS_PROPERTY,
                "body": _BODY_PROPERTY,
            },
            "required": ["body"],
            "anyOf": _TEMPLATE_SLUG_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_draft_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.runbook.edit_template",
        description=_EDIT_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "slug": _SLUG_ALIAS_PROPERTY,
                "body": _BODY_PROPERTY,
            },
            "required": ["body"],
            "anyOf": _TEMPLATE_SLUG_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_edit_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.runbook.publish_template",
        description=_PUBLISH_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "slug": _SLUG_ALIAS_PROPERTY,
                "version": _VERSION_PROPERTY,
            },
            "required": ["version"],
            "anyOf": _TEMPLATE_SLUG_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_publish_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.runbook.deprecate_template",
        description=_DEPRECATE_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "slug": _SLUG_ALIAS_PROPERTY,
                "version": _VERSION_PROPERTY,
            },
            "required": ["version"],
            "anyOf": _TEMPLATE_SLUG_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.TENANT_ADMIN,
        op_class=_OP_CLASS_WRITE,
    ),
    handler=_deprecate_template_handler,
)


register_mcp_tool(
    definition=ToolDefinition(
        name="meho.runbook.list_templates",
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
        name="meho.runbook.show_template",
        description=_SHOW_DESCRIPTION,
        inputSchema={
            "type": "object",
            "properties": {
                "template_slug": _TEMPLATE_SLUG_PROPERTY,
                "slug": _SLUG_ALIAS_PROPERTY,
                "version": {
                    **_VERSION_PROPERTY,
                    "description": (
                        "Optional template version. Omit to return the latest version."
                    ),
                },
            },
            # No `required` array — the template id is the only required
            # input and it is accepted under either alias name, so the
            # `anyOf` is the only required-shape declaration (the
            # broadcast.watch cursor/since_cursor precedent, §14.2).
            "anyOf": _TEMPLATE_SLUG_ANYOF,
            "additionalProperties": False,
        },
        required_role=TenantRole.OPERATOR,
        op_class=_OP_CLASS_READ,
    ),
    handler=_show_template_handler,
)


# ---------------------------------------------------------------------------
# Deprecated flat-name aliases (#1612) — one release, removed in
# v0.14.0 (`_ALIAS_REMOVAL_VERSION`). Registered strictly after their
# canonical targets; each shares the canonical handler object and
# schema, differing only in name + DEPRECATED pointer description.
# ---------------------------------------------------------------------------


for _flat_alias, _canonical in (
    ("runbook_draft_template", "meho.runbook.draft_template"),
    ("runbook_edit_template", "meho.runbook.edit_template"),
    ("runbook_publish_template", "meho.runbook.publish_template"),
    ("runbook_deprecate_template", "meho.runbook.deprecate_template"),
    ("runbook_list_templates", "meho.runbook.list_templates"),
    ("runbook_show_template", "meho.runbook.show_template"),
):
    register_deprecated_mcp_tool_alias(
        alias=_flat_alias,
        canonical=_canonical,
        removal_version=_ALIAS_REMOVAL_VERSION,
    )
